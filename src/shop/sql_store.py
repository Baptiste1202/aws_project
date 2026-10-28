"""Accès aux données PostgreSQL : solution VM et écritures de la solution hybride (RDS).

Toutes les écritures de stock sont des transactions SQL. Les lignes de stock sont
toujours verrouillées dans le même ordre (produit, entrepôt) : pas d'interblocage
entre commandes, réservations et mouvements simultanés.

Les méthodes d'écriture renvoient un dict « changements » que l'API transforme en
événements : order, stock (niveaux avec version), movements, alerts, products.
"""

import json
import threading
import time

import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool

from shop import domain
from shop.domain import ApiError, WAREHOUSES

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id                  TEXT PRIMARY KEY,
    category            TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL,
    price               NUMERIC(10, 2) NOT NULL,
    color               TEXT NOT NULL,
    hex                 TEXT NOT NULL,
    sizes               JSONB NOT NULL,
    cost_price          NUMERIC(10, 2) NOT NULL DEFAULT 0,
    low_stock_threshold INTEGER NOT NULL DEFAULT 5,
    archived            BOOLEAN NOT NULL DEFAULT FALSE,
    version             BIGINT NOT NULL DEFAULT 0
);
ALTER TABLE products ADD COLUMN IF NOT EXISTS cost_price NUMERIC(10, 2) NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN IF NOT EXISTS low_stock_threshold INTEGER NOT NULL DEFAULT 5;
ALTER TABLE products ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE products ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;
-- Le stock est maintenant par entrepôt (table stock).
ALTER TABLE products DROP COLUMN IF EXISTS stock;

CREATE TABLE IF NOT EXISTS stock (
    product_id TEXT NOT NULL REFERENCES products(id),
    warehouse  TEXT NOT NULL,
    on_hand    INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved   INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    version    BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, warehouse),
    CHECK (reserved <= on_hand)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    movement_id   TEXT PRIMARY KEY,
    product_id    TEXT NOT NULL,
    warehouse     TEXT NOT NULL,
    type          TEXT NOT NULL,
    delta         INTEGER NOT NULL,
    on_hand_after INTEGER NOT NULL,
    ref           TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS stock_movements_at ON stock_movements (at DESC);
-- Une annulation ne remet le stock qu'une fois, même rejouée.
CREATE UNIQUE INDEX IF NOT EXISTS stock_movements_cancel_once
    ON stock_movements (ref, product_id, warehouse) WHERE type = 'CANCEL';

CREATE TABLE IF NOT EXISTS reservations (
    cart_id     TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    allocations JSONB NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (cart_id, product_id)
);
CREATE INDEX IF NOT EXISTS reservations_expires ON reservations (expires_at);

CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    customer    JSONB NOT NULL,
    items       JSONB NOT NULL,
    total       NUMERIC(10, 2) NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    history     JSONB NOT NULL,
    shipping    JSONB,
    allocations JSONB NOT NULL DEFAULT '[]'
);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS allocations JSONB NOT NULL DEFAULT '[]';

-- Lot 2a : comptes, codes promo, avis, liste de souhaits, notifications
ALTER TABLE products ADD COLUMN IF NOT EXISTS rating_sum INTEGER NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN IF NOT EXISTS rating_count INTEGER NOT NULL DEFAULT 0;
-- Lot 2b : image produit (miniatures images/<produit>/<image_id>-<taille>.webp)
ALTER TABLE products ADD COLUMN IF NOT EXISTS image_id TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS subtotal NUMERIC(10, 2);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS discount NUMERIC(10, 2) NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS promo_code TEXT;
CREATE INDEX IF NOT EXISTS orders_user ON orders (user_id, created_at DESC);
-- Lot 3a : paiement Stripe (PENDING_PAYMENT jusqu'au paiement), frais de port.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_method TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_fee NUMERIC(10, 2) NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment JSONB;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_deadline TEXT;
CREATE INDEX IF NOT EXISTS orders_pending ON orders (payment_deadline) WHERE status = 'PENDING_PAYMENT';
-- Lot 3b : retours (droit de rétractation), bons de commande fournisseur.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_request JSONB;
CREATE INDEX IF NOT EXISTS orders_returns ON orders (updated_at) WHERE status = 'RETURN_REQUESTED';
-- Un retour ne remet le stock qu'une fois, même rejoué.
CREATE UNIQUE INDEX IF NOT EXISTS stock_movements_return_once
    ON stock_movements (ref, product_id, warehouse) WHERE type = 'RETURN';

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id       TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL REFERENCES products(id),
    name        TEXT NOT NULL,
    supplier    TEXT NOT NULL,
    warehouse   TEXT NOT NULL,
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    status      TEXT NOT NULL CHECK (status IN ('OPEN', 'RECEIVED')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    received_at TEXT,
    alert       JSONB,
    version     BIGINT NOT NULL DEFAULT 1
);
-- Un seul bon ouvert par produit : deux alertes simultanées ne commandent qu'une fois.
CREATE UNIQUE INDEX IF NOT EXISTS purchase_orders_open ON purchase_orders (product_id) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS purchase_orders_created ON purchase_orders (created_at DESC);

CREATE TABLE IF NOT EXISTS promos (
    code       TEXT PRIMARY KEY,
    type       TEXT NOT NULL CHECK (type IN ('percent', 'fixed')),
    value      NUMERIC(10, 2) NOT NULL CHECK (value > 0),
    min_total  NUMERIC(10, 2) NOT NULL DEFAULT 0,
    max_uses   INTEGER,
    uses       INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    version    BIGINT NOT NULL DEFAULT 1,
    CHECK (max_uses IS NULL OR uses <= max_uses)
);

CREATE TABLE IF NOT EXISTS reviews (
    product_id TEXT NOT NULL REFERENCES products(id),
    user_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    rating     INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment    TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL,
    PRIMARY KEY (product_id, user_id)
);

CREATE TABLE IF NOT EXISTS wishlists (
    user_id    TEXT NOT NULL,
    product_id TEXT NOT NULL REFERENCES products(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, product_id)
);
CREATE INDEX IF NOT EXISTS wishlists_product ON wishlists (product_id);
-- Lot 4 : adresse du client, pour l'e-mail de retour en stock.
ALTER TABLE wishlists ADD COLUMN IF NOT EXISTS email TEXT;

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    type            TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    message         TEXT NOT NULL,
    at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications (user_id, at DESC);
"""

POOL_SIZE = 10
POOL_WAIT_SECONDS = 10


def log(**fields):
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


def _product(row):
    p = {k: row[k] for k in ("id", "category", "name", "description", "price", "color", "hex", "sizes",
                             "cost_price", "low_stock_threshold", "archived", "version", "rating_sum",
                             "rating_count", "image_id")}
    return domain.with_rating(p)


def _promo(row):
    return {k: row[k] for k in ("code", "type", "value", "min_total", "max_uses", "uses", "expires_at", "active",
                                "version")}


class SqlStore:
    def __init__(self, host, dbname, user, password, backend="vm", status_store=None):
        """status_store : en hybride, le statut des commandes fait foi dans DynamoDB
        (DynamoStore) ; l'annulation y passe la commande à CANCELLED avant de remettre
        le stock dans RDS."""
        self.params = {"host": host, "dbname": dbname, "user": user, "password": password}
        self.backend = backend
        self.status_store = status_store
        self.pool = None
        # psycopg2 lève une erreur quand le pool est vide : ce sémaphore fait attendre
        # la requête qu'une connexion se libère (au plus POOL_WAIT_SECONDS).
        self.slots = threading.BoundedSemaphore(POOL_SIZE)

    # --- connexion et initialisation -----------------------------------------

    def _create_database(self):
        """Crée la base si elle n'existe pas (VM et hybride ont chacune la leur, sur la
        même instance RDS)."""
        conn = psycopg2.connect(**{**self.params, "dbname": "postgres"}, connect_timeout=5)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (self.params["dbname"],))
                if not cur.fetchone():
                    cur.execute(f'CREATE DATABASE "{self.params["dbname"]}"')
                    log(msg="base créée", db=self.params["dbname"])
        except psycopg2.errors.DuplicateDatabase:
            pass  # créée au même moment par une autre instance
        finally:
            conn.close()

    def connect(self, products):
        """Ouvre le pool (en attendant la base), crée le schéma et charge le catalogue initial."""
        while True:
            try:
                self.pool = psycopg2.pool.ThreadedConnectionPool(1, POOL_SIZE, connect_timeout=5, **self.params)
                break
            except psycopg2.OperationalError as e:
                if "does not exist" in str(e):
                    self._create_database()
                    continue
                log(msg="base indisponible, nouvel essai dans 5 s", error=str(e))
                time.sleep(5)
        with self.tx() as cur:
            # Verrou applicatif : plusieurs instances peuvent démarrer en même temps.
            cur.execute("SELECT pg_advisory_xact_lock(42)")
            cur.execute(SCHEMA)
            for raw in products:
                p = domain.seed_product(raw)
                cur.execute(
                    """INSERT INTO products (id, category, name, description, price, color, hex, sizes,
                                             cost_price, low_stock_threshold, archived)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                    (p["id"], p["category"], p["name"], p["description"], p["price"], p["color"], p["hex"],
                     json.dumps(p["sizes"]), p["cost_price"], p["low_stock_threshold"], p["archived"]))
                for wh, qty in domain.initial_split(raw["stock"]).items():
                    cur.execute("""INSERT INTO stock (product_id, warehouse, on_hand) VALUES (%s, %s, %s)
                                   ON CONFLICT DO NOTHING""", (p["id"], wh, qty))
        log(msg="base initialisée", products=len(products))

    def tx(self):
        return _Transaction(self)

    # --- lectures -----------------------------------------------------------

    def _catalog(self, cur, ids=None, include_archived=False, lock=False):
        """Produits {id: produit} et niveaux de stock {(id, entrepôt): niveau}."""
        where, args = [], []
        if ids is not None:
            where.append("id = ANY(%s)")
            args.append(list(ids))
        if not include_archived:
            where.append("NOT archived")
        cur.execute("SELECT * FROM products" + (" WHERE " + " AND ".join(where) if where else "") +
                    " ORDER BY id", args)
        products = {r["id"]: _product(r) for r in cur.fetchall()}
        levels = self._levels(cur, list(products), lock=lock)
        return products, levels

    def _levels(self, cur, product_ids, lock=False):
        cur.execute("SELECT * FROM stock WHERE product_id = ANY(%s) ORDER BY product_id, warehouse"
                    + (" FOR UPDATE" if lock else ""), (list(product_ids),))
        return {(r["product_id"], r["warehouse"]): domain.level(r["product_id"], r["warehouse"], r["on_hand"],
                                                                r["reserved"], r["version"])
                for r in cur.fetchall()}

    @staticmethod
    def _with_stock(products, levels):
        return [domain.with_stock(p, [lv for (pid, _), lv in levels.items() if pid == p["id"]])
                for p in products.values()]

    def list_products(self, category=None, include_archived=False):
        with self.tx() as cur:
            products, levels = self._catalog(cur, include_archived=include_archived)
        out = self._with_stock(products, levels)
        return [p for p in out if not category or p["category"] == category]

    def get_product(self, product_id, include_archived=False):
        with self.tx() as cur:
            products, levels = self._catalog(cur, [product_id], include_archived=include_archived)
        if not products:
            raise ApiError(404, "Produit introuvable")
        return self._with_stock(products, levels)[0]

    def get_order(self, order_id):
        with self.tx() as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            row = cur.fetchone()
        if not row:
            raise ApiError(404, "Commande introuvable")
        order = self._order(row)
        if self.status_store:  # hybride : statut, historique et expédition font foi dans DynamoDB
            try:
                current = self.status_store.get_order(order_id)
                order.update({k: current[k] for k in ("status", "history", "updated_at", "shipping", "return")
                              if k in current})
            except ApiError:
                pass  # pas encore projetée : l'état de RDS est le plus récent
        return order

    def _order(self, row):
        row = dict(row)
        row["backend"] = self.backend
        row["return"] = row.pop("return_request", None)
        for k in ("shipping", "payment", "payment_deadline", "return"):
            if row.get(k) is None:
                row.pop(k, None)
        return row

    def list_orders(self, since):
        """Commandes créées depuis since (tableau de bord, rapport). Hybride : RDS suit
        le paiement, l'annulation et le retour, ce qui suffit au chiffre d'affaires."""
        with self.tx() as cur:
            cur.execute("SELECT * FROM orders WHERE created_at >= %s ORDER BY created_at", (since,))
            return [self._order(r) for r in cur.fetchall()]

    def list_movements(self, product_id=None, limit=100):
        with self.tx() as cur:
            if product_id:
                cur.execute("SELECT * FROM stock_movements WHERE product_id = %s ORDER BY at DESC, movement_id "
                            "LIMIT %s", (product_id, limit))
            else:
                cur.execute("SELECT * FROM stock_movements ORDER BY at DESC, movement_id LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def valuation(self):
        with self.tx() as cur:
            products, levels = self._catalog(cur, include_archived=True)
        return domain.valuation(products, list(levels.values()))

    def low_stock(self):
        return [p for p in self.list_products() if p["stock"] < p["low_stock_threshold"]]

    # --- écritures : outils ----------------------------------------------------

    def _write_levels(self, cur, levels, changed):
        """Écrit les niveaux modifiés (version + 1). Renvoie leurs nouvelles valeurs."""
        out = []
        for key in sorted(changed):
            lv = levels[key]
            cur.execute("""UPDATE stock SET on_hand = %s, reserved = %s, version = version + 1
                           WHERE product_id = %s AND warehouse = %s RETURNING *""",
                        (lv["on_hand"], lv["reserved"], key[0], key[1]))
            r = cur.fetchone()
            levels[key] = domain.level(r["product_id"], r["warehouse"], r["on_hand"], r["reserved"], r["version"])
            out.append(levels[key])
        return out

    @staticmethod
    def _totals(levels, product_ids):
        return {pid: sum(lv["available"] for (p, _), lv in levels.items() if p == pid) for pid in product_ids}

    @staticmethod
    def _adjust(levels, key, on_hand=0, reserved=0):
        lv = levels[key]
        lv["on_hand"] += on_hand
        lv["reserved"] += reserved
        lv["available"] = lv["on_hand"] - lv["reserved"]

    def _insert_movements(self, cur, movements):
        for m in movements:
            cur.execute("""INSERT INTO stock_movements (movement_id, product_id, warehouse, type, delta,
                                                        on_hand_after, ref, reason, at)
                           VALUES (%(movement_id)s, %(product_id)s, %(warehouse)s, %(type)s, %(delta)s,
                                   %(on_hand_after)s, %(ref)s, %(reason)s, %(at)s)""", m)

    def _cart_holds(self, cur, cart_id, lock=True):
        """Réservations du panier : {(produit, entrepôt): quantité}. Une réservation
        expirée mais pas encore purgée compte aussi : elle est supprimée avec le panier,
        son stock réservé doit donc être utilisé ou libéré ici."""
        cur.execute("SELECT * FROM reservations WHERE cart_id = %s ORDER BY product_id"
                    + (" FOR UPDATE" if lock else ""), (cart_id,))
        held = {}
        for r in cur.fetchall():
            for a in r["allocations"]:
                held[(r["product_id"], a["warehouse"])] = held.get((r["product_id"], a["warehouse"]), 0) + a["quantity"]
        return held

    # --- commandes ----------------------------------------------------------

    def create_order(self, customer, lines, per_product, cart_id=None, promo_code=None, user=None,
                     shipping_method="standard", payment_ttl=domain.PAYMENT_TTL):
        """Transaction : contrôle et décrément du stock (réservations du panier d'abord,
        puis Paris, puis Lyon), mouvements SALE, code promo (verrouillé et compté dans la
        même transaction : jamais plus d'utilisations que permis), commande. Les
        réservations restantes du panier sont libérées. La commande attend son paiement
        (PENDING_PAYMENT) jusqu'à payment_deadline."""
        order_id = domain.new_order_id()
        with self.tx() as cur:
            promo = None
            if promo_code:
                cur.execute("SELECT * FROM promos WHERE code = %s FOR UPDATE", (promo_code,))
                row = cur.fetchone()
                promo = _promo(row) if row else None
            held = self._cart_holds(cur, cart_id) if cart_id else {}
            ids = set(per_product) | {pid for pid, _ in held}
            products, levels = self._catalog(cur, ids, include_archived=True, lock=True)
            for pid in per_product:
                if pid in products and products[pid]["archived"]:
                    raise ApiError(400, f"Produit indisponible : {pid}")
            items, total = domain.price_lines(lines, products)
            before = self._totals(levels, ids)
            free = {k: lv["available"] for k, lv in levels.items()}
            allocations = domain.allocate(per_product, free, held)
            changed, movements = set(), []
            for a in allocations:
                key = (a["product_id"], a["warehouse"])
                self._adjust(levels, key, on_hand=-a["quantity"], reserved=-a["from_reservation"])
                held[key] = held.get(key, 0) - a["from_reservation"]
                changed.add(key)
            for key, left in held.items():  # réservations non utilisées : libérées
                if left > 0:
                    self._adjust(levels, key, reserved=-left)
                    changed.add(key)
            if cart_id:
                cur.execute("DELETE FROM reservations WHERE cart_id = %s", (cart_id,))
            stock = self._write_levels(cur, levels, changed)
            for a in allocations:
                lv = levels[(a["product_id"], a["warehouse"])]
                movements.append(domain.new_movement(a["product_id"], a["warehouse"], "SALE", -a["quantity"],
                                                     lv["on_hand"], ref=order_id))
            self._insert_movements(cur, movements)
            order = domain.new_order(order_id, customer, items, total, self.backend)
            order["allocations"] = [{k: a[k] for k in ("product_id", "warehouse", "quantity")} for a in allocations]
            discount = domain.promo_discount(promo, total) if promo_code else 0
            if promo_code:
                # version + 1 : la projection de l'hybride n'applique que les versions plus récentes.
                cur.execute("UPDATE promos SET uses = uses + 1, version = version + 1 WHERE code = %s", (promo_code,))
            domain.apply_totals(order, discount, promo_code, shipping_method)
            order["user_id"] = user["sub"] if user else None
            order["payment_deadline"] = domain.payment_deadline(payment_ttl)
            cur.execute(
                """INSERT INTO orders (order_id, status, customer, items, total, created_at, updated_at, history,
                                       allocations, user_id, subtotal, discount, promo_code, shipping_method,
                                       shipping_fee, payment_deadline)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (order_id, order["status"], json.dumps(customer), json.dumps(items, default=domain.to_json),
                 order["total"], order["created_at"], order["updated_at"], json.dumps(order["history"]),
                 json.dumps(order["allocations"]), order["user_id"], order["subtotal"], order["discount"],
                 promo_code, shipping_method, order["shipping_fee"], order["payment_deadline"]))
            alerts = domain.low_stock_crossings(products, before, self._totals(levels, ids))
            promos = []
            if promo_code:
                cur.execute("SELECT * FROM promos WHERE code = %s", (promo_code,))
                promos = [_promo(cur.fetchone())]
        return {"order": order, "stock": stock, "movements": movements, "alerts": alerts, "promos": promos}

    def cancel_order(self, order_id):
        """Annule une commande pas encore expédiée et remet son stock (mouvements CANCEL)."""
        if self.status_store:
            # Hybride : le statut fait foi dans DynamoDB ; la remise en stock dans RDS est
            # idempotente (rejouer l'annulation ne remet pas le stock deux fois). Annulée
            # juste après sa création, la commande peut ne pas être encore projetée :
            # l'EC2 l'écrit d'abord depuis RDS (écriture conditionnelle, comme le processor).
            with self.tx() as cur:
                cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
                row = cur.fetchone()
            if not row:
                raise ApiError(404, "Commande introuvable")
            self.status_store.create_order_if_absent(self._order(row))
            order, already = self.status_store.mark_cancelled(order_id)
            with self.tx() as cur:
                restored = self._restore(cur, order_id, row["allocations"])
                if already and restored is None:
                    raise ApiError(409, "Commande non annulable (statut CANCELLED)")
                cur.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = %s", (order_id,))
                stock, movements, restocked = restored or ([], [], [])
            return {"order": order, "stock": stock, "movements": movements, "restocked": restocked}
        with self.tx() as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
            row = cur.fetchone()
            if not row:
                raise ApiError(404, "Commande introuvable")
            if row["status"] not in domain.CANCELLABLE:
                raise ApiError(409, f"Commande non annulable (statut {row['status']})")
            now = domain.now_iso()
            cur.execute("""UPDATE orders SET status = 'CANCELLED', updated_at = %s, history = history || %s::jsonb
                           WHERE order_id = %s RETURNING *""",
                        (now, json.dumps([{"status": "CANCELLED", "at": now}]), order_id))
            order = self._order(cur.fetchone())
            stock, movements, restocked = self._restore(cur, order_id, row["allocations"]) or ([], [], [])
        return {"order": order, "stock": stock, "movements": movements, "restocked": restocked}

    # --- paiement (lot 3a) ----------------------------------------------------

    def set_payment(self, order_id, payment):
        with self.tx() as cur:
            cur.execute("UPDATE orders SET payment = %s WHERE order_id = %s RETURNING *",
                        (json.dumps(payment), order_id))
            row = cur.fetchone()
        if self.status_store:  # hybride : si la projection est déjà passée
            self.status_store.set_payment(order_id, payment)
        return self._order(row)

    def mark_paid(self, order_id, payment=None):
        """PENDING_PAYMENT -> CONFIRMED, une seule fois. Renvoie (commande, déjà payée)."""
        if self.status_store:
            # Hybride : le statut fait foi dans DynamoDB (la commande y est écrite depuis
            # RDS si la projection n'est pas encore passée) ; RDS suit, pour la purge.
            row_order = self.get_order(order_id)
            self.status_store.create_order_if_absent({**row_order, "status": "PENDING_PAYMENT"})
            order, already = self.status_store.mark_paid(order_id, payment)
            with self.tx() as cur:
                cur.execute("""UPDATE orders SET status = 'CONFIRMED', payment = COALESCE(%s::jsonb, payment)
                               WHERE order_id = %s AND status = 'PENDING_PAYMENT'""",
                            (json.dumps(payment) if payment else None, order_id))
            return order, already
        now = domain.now_iso()
        with self.tx() as cur:
            cur.execute("""UPDATE orders SET status = 'CONFIRMED', updated_at = %s, history = history || %s::jsonb,
                                  payment = COALESCE(%s::jsonb, payment)
                           WHERE order_id = %s AND status = 'PENDING_PAYMENT' RETURNING *""",
                        (now, json.dumps([{"status": "CONFIRMED", "at": now}]),
                         json.dumps(payment) if payment else None, order_id))
            row = cur.fetchone()
            if row:
                return self._order(row), False
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            row = cur.fetchone()
        if not row:
            raise ApiError(404, "Commande introuvable")
        if row["status"] in domain.PAID_STATUSES:
            return self._order(row), True
        raise ApiError(409, f"Commande non payable (statut {row['status']})")

    def pending_payments(self, now_iso, limit=50):
        """Commandes impayées dont le délai de paiement est dépassé (purge)."""
        with self.tx() as cur:
            cur.execute("""SELECT * FROM orders WHERE status = 'PENDING_PAYMENT' AND payment_deadline <= %s
                           ORDER BY payment_deadline LIMIT %s""", (now_iso, limit))
            return [self._order(r) for r in cur.fetchall()]

    def _restore(self, cur, order_id, allocations, kind="CANCEL"):
        """Remet en stock une commande annulée (CANCEL) ou retournée (RETURN). Renvoie
        None si c'est déjà fait."""
        cur.execute("SELECT 1 FROM stock_movements WHERE ref = %s AND type = %s LIMIT 1", (order_id, kind))
        ids = {a["product_id"] for a in allocations}
        if cur.fetchone():  # déjà remis en stock
            return None
        _, levels = self._catalog(cur, ids, include_archived=True, lock=True)
        before = self._totals(levels, ids)
        changed, movements = set(), []
        for a in allocations:
            key = (a["product_id"], a["warehouse"])
            self._adjust(levels, key, on_hand=a["quantity"])
            changed.add(key)
        stock = self._write_levels(cur, levels, changed)
        for a in allocations:
            lv = levels[(a["product_id"], a["warehouse"])]
            movements.append(domain.new_movement(a["product_id"], a["warehouse"], kind, a["quantity"],
                                                 lv["on_hand"], ref=order_id))
        self._insert_movements(cur, movements)
        return stock, movements, domain.restock_crossings(before, self._totals(levels, ids))

    # --- retours (lot 3b) ----------------------------------------------------

    def request_return(self, order_id, ret):
        """DELIVERED -> RETURN_REQUESTED (hybride : dans DynamoDB, où le statut fait foi)."""
        if self.status_store:
            result = self.status_store.request_return(order_id, ret)
            with self.tx() as cur:  # RDS suit, pour la trace
                cur.execute("UPDATE orders SET status = 'RETURN_REQUESTED', return_request = %s WHERE order_id = %s",
                            (json.dumps(ret), order_id))
            return {"order": self.get_order(order_id)}
        now = domain.now_iso()
        with self.tx() as cur:
            cur.execute("""UPDATE orders SET status = 'RETURN_REQUESTED', updated_at = %s, return_request = %s,
                                  history = history || %s::jsonb
                           WHERE order_id = %s AND status = 'DELIVERED' RETURNING *""",
                        (now, json.dumps(ret), json.dumps([{"status": "RETURN_REQUESTED", "at": now}]), order_id))
            row = cur.fetchone()
        if not row:
            raise ApiError(409, f"Retour impossible pour une commande {self.get_order(order_id)['status']}")
        return {"order": self._order(row)}

    def receive_return(self, order_id):
        """Réception d'un retour : RETURNED et remise en stock (mouvements RETURN).
        already=True si c'était déjà fait (l'API reprend alors seulement le remboursement)."""
        if self.status_store:
            # Hybride, comme l'annulation : statut dans DynamoDB d'abord, puis stock dans
            # RDS (idempotent : une réception interrompue est reprise sans doublon).
            with self.tx() as cur:
                cur.execute("SELECT allocations FROM orders WHERE order_id = %s", (order_id,))
                row = cur.fetchone()
            if not row:
                raise ApiError(404, "Commande introuvable")
            order, already = self.status_store.mark_returned(order_id)
            with self.tx() as cur:
                restored = self._restore(cur, order_id, row["allocations"], "RETURN")
                cur.execute("UPDATE orders SET status = 'RETURNED', return_request = %s WHERE order_id = %s",
                            (json.dumps(order.get("return"), default=domain.to_json), order_id))
            stock, movements, restocked = restored or ([], [], [])
            return {"order": self.get_order(order_id), "stock": stock, "movements": movements,
                    "restocked": restocked, "already": already and restored is None}
        with self.tx() as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
            row = cur.fetchone()
            if not row:
                raise ApiError(404, "Commande introuvable")
            if row["status"] == "RETURNED":
                return {"order": self._order(row), "stock": [], "movements": [], "restocked": [], "already": True}
            if row["status"] != "RETURN_REQUESTED":
                raise ApiError(409, f"Aucun retour à réceptionner (statut {row['status']})")
            now = domain.now_iso()
            cur.execute("""UPDATE orders SET status = 'RETURNED', updated_at = %s, history = history || %s::jsonb,
                                  return_request = return_request || %s::jsonb
                           WHERE order_id = %s RETURNING *""",
                        (now, json.dumps([{"status": "RETURNED", "at": now}]), json.dumps({"received_at": now}),
                         order_id))
            order = self._order(cur.fetchone())
            stock, movements, restocked = self._restore(cur, order_id, row["allocations"], "RETURN") or ([], [], [])
        return {"order": order, "stock": stock, "movements": movements, "restocked": restocked, "already": False}

    def list_returns(self, limit=100):
        with self.tx() as cur:
            cur.execute("SELECT * FROM orders WHERE status = 'RETURN_REQUESTED' ORDER BY updated_at LIMIT %s", (limit,))
            return [self._order(r) for r in cur.fetchall()]

    # --- réapprovisionnement fournisseur (lot 3b) -----------------------------

    def open_purchase_order(self, product_id, alert=None):
        """Bon de commande du produit, sauf s'il en a déjà un ouvert (index unique
        partiel). Renvoie (bon, créé)."""
        with self.tx() as cur:
            products, _ = self._catalog(cur, [product_id], include_archived=True)
            if not products:
                raise ApiError(404, "Produit introuvable")
            po = domain.new_purchase_order(products[product_id], alert)
            cur.execute("""INSERT INTO purchase_orders (po_id, product_id, name, supplier, warehouse, quantity, status,
                                                        created_at, updated_at, alert, version)
                           VALUES (%(po_id)s, %(product_id)s, %(name)s, %(supplier)s, %(warehouse)s, %(quantity)s,
                                   %(status)s, %(created_at)s, %(updated_at)s, %(alert_json)s, %(version)s)
                           ON CONFLICT (product_id) WHERE status = 'OPEN' DO NOTHING RETURNING *""",
                        {**po, "alert_json": json.dumps(po["alert"]) if po["alert"] else None})
            row = cur.fetchone()
            if row:
                return dict(row), True
            cur.execute("SELECT * FROM purchase_orders WHERE product_id = %s AND status = 'OPEN'", (product_id,))
            row = cur.fetchone()
        if not row:
            raise ApiError(409, "Bon de commande en cours de création, réessayez")
        return dict(row), False

    def receive_purchase_order(self, po_id):
        """Livraison du fournisseur : stock + quantité (mouvement RESTOCK de référence le
        bon), bon RECEIVED. Rejouée : already=True, rien n'est refait."""
        with self.tx() as cur:
            cur.execute("SELECT * FROM purchase_orders WHERE po_id = %s FOR UPDATE", (po_id,))
            row = cur.fetchone()
            if not row:
                raise ApiError(404, "Bon de commande introuvable")
            if row["status"] == "RECEIVED":
                return {"purchase_orders": [dict(row)], "stock": [], "movements": [], "restocked": [], "already": True}
            pid, key = row["product_id"], (row["product_id"], row["warehouse"])
            _, levels = self._catalog(cur, [pid], include_archived=True, lock=True)
            before = self._totals(levels, [pid])
            self._adjust(levels, key, on_hand=row["quantity"])
            stock = self._write_levels(cur, levels, {key})
            movements = [domain.new_movement(*key, "RESTOCK", row["quantity"], levels[key]["on_hand"], ref=po_id,
                                             reason=f"Livraison fournisseur {row['supplier']}")]
            self._insert_movements(cur, movements)
            now = domain.now_iso()
            cur.execute("""UPDATE purchase_orders SET status = 'RECEIVED', received_at = %s, updated_at = %s,
                                  version = version + 1
                           WHERE po_id = %s RETURNING *""", (now, now, po_id))
            po = dict(cur.fetchone())
            restocked = domain.restock_crossings(before, self._totals(levels, [pid]))
        return {"purchase_orders": [po], "stock": stock, "movements": movements, "restocked": restocked,
                "already": False}

    def list_purchase_orders(self, limit=100):
        with self.tx() as cur:
            cur.execute("SELECT * FROM purchase_orders ORDER BY created_at DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # --- réservations -------------------------------------------------------

    def reserve(self, cart_id, product_id, quantity, ttl):
        """Fixe la quantité réservée d'un produit pour un panier (0 = libère)."""
        with self.tx() as cur:
            # Même ordre de verrouillage que create_order : réservation, puis stock.
            cur.execute("SELECT * FROM reservations WHERE cart_id = %s AND product_id = %s FOR UPDATE",
                        (cart_id, product_id))
            row = cur.fetchone()
            products, levels = self._catalog(cur, [product_id], include_archived=True, lock=True)
            if not products or products[product_id]["archived"]:
                raise ApiError(404, "Produit introuvable")
            current = {a["warehouse"]: a["quantity"] for a in row["allocations"]} if row else {}
            before = self._totals(levels, [product_id])
            changed = set()
            delta = quantity - sum(current.values())
            if delta > 0:
                free = {k: lv["available"] for k, lv in levels.items()}
                for a in domain.allocate({product_id: delta}, free):
                    current[a["warehouse"]] = current.get(a["warehouse"], 0) + a["quantity"]
                    self._adjust(levels, (product_id, a["warehouse"]), reserved=a["quantity"])
                    changed.add((product_id, a["warehouse"]))
            elif delta < 0:
                give_back = -delta
                for wh in reversed(WAREHOUSES):  # on libère d'abord Lyon, puis Paris
                    use = min(current.get(wh, 0), give_back)
                    if use:
                        current[wh] -= use
                        give_back -= use
                        self._adjust(levels, (product_id, wh), reserved=-use)
                        changed.add((product_id, wh))
            allocations = [{"warehouse": wh, "quantity": q} for wh, q in current.items() if q > 0]
            if quantity == 0:
                cur.execute("DELETE FROM reservations WHERE cart_id = %s AND product_id = %s", (cart_id, product_id))
            else:
                cur.execute("""INSERT INTO reservations (cart_id, product_id, quantity, allocations, expires_at)
                               VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
                               ON CONFLICT (cart_id, product_id) DO UPDATE SET quantity = EXCLUDED.quantity,
                                   allocations = EXCLUDED.allocations, expires_at = EXCLUDED.expires_at""",
                            (cart_id, product_id, quantity, json.dumps(allocations), ttl))
            stock = self._write_levels(cur, levels, changed)
            after = self._totals(levels, [product_id])
            alerts = domain.low_stock_crossings(products, before, after)
        reservation = {"cart_id": cart_id, "product_id": product_id, "quantity": quantity,
                       "allocations": allocations, "expires_at": domain.expires_at(ttl) if quantity else None}
        return {"reservation": reservation, "stock": stock, "alerts": alerts,
                "restocked": domain.restock_crossings(before, after)}

    def release(self, cart_id, product_id=None):
        with self.tx() as cur:
            return self._release_rows(cur, "cart_id = %s" + (" AND product_id = %s" if product_id else ""),
                                      (cart_id, product_id) if product_id else (cart_id,))

    def expire_reservations(self):
        """Libère les réservations expirées (appelé périodiquement sur les EC2)."""
        with self.tx() as cur:
            return self._release_rows(cur, "expires_at <= now()", (), skip_locked=True)

    def _release_rows(self, cur, where, args, skip_locked=False):
        cur.execute(f"SELECT * FROM reservations WHERE {where} ORDER BY cart_id, product_id FOR UPDATE"
                    + (" SKIP LOCKED" if skip_locked else ""), args)
        rows = cur.fetchall()
        if not rows:
            return {"stock": [], "restocked": []}
        ids = {r["product_id"] for r in rows}
        _, levels = self._catalog(cur, ids, include_archived=True, lock=True)
        before = self._totals(levels, ids)
        changed = set()
        for r in rows:
            for a in r["allocations"]:
                self._adjust(levels, (r["product_id"], a["warehouse"]), reserved=-a["quantity"])
                changed.add((r["product_id"], a["warehouse"]))
            cur.execute("DELETE FROM reservations WHERE cart_id = %s AND product_id = %s",
                        (r["cart_id"], r["product_id"]))
        stock = self._write_levels(cur, levels, changed)
        return {"stock": stock, "restocked": domain.restock_crossings(before, self._totals(levels, ids))}

    # --- administration -----------------------------------------------------

    def apply_movement(self, product_id, warehouse, kind, quantity, reason=""):
        """Réappro, casse ou inventaire. Le stock physique ne peut pas passer sous le réservé."""
        with self.tx() as cur:
            products, levels = self._catalog(cur, [product_id], include_archived=True, lock=True)
            if not products:
                raise ApiError(404, "Produit introuvable")
            key = (product_id, warehouse)
            before = self._totals(levels, [product_id])
            delta = domain.movement_delta(kind, quantity, levels[key]["on_hand"])
            if levels[key]["on_hand"] + delta < levels[key]["reserved"]:
                raise ApiError(409, "Stock insuffisant : des unités sont réservées ou le stock deviendrait négatif")
            self._adjust(levels, key, on_hand=delta)
            stock = self._write_levels(cur, levels, {key})
            movements = [domain.new_movement(product_id, warehouse, kind, delta, levels[key]["on_hand"],
                                             reason=reason)]
            self._insert_movements(cur, movements)
            after = self._totals(levels, [product_id])
            alerts = domain.low_stock_crossings(products, before, after)
        return {"stock": stock, "movements": movements, "alerts": alerts,
                "restocked": domain.restock_crossings(before, after)}

    def create_product(self, product_id, fields):
        with self.tx() as cur:
            cur.execute("SELECT 1 FROM products WHERE id = %s", (product_id,))
            if cur.fetchone():
                raise ApiError(409, f"Le produit {product_id} existe déjà")
            self._insert_product(cur, product_id, fields)
            products, levels = self._catalog(cur, [product_id], include_archived=True)
        return {"products": self._with_stock(products, levels), "stock": list(levels.values())}

    def _insert_product(self, cur, product_id, f):
        cur.execute("""INSERT INTO products (id, category, name, description, price, color, hex, sizes, cost_price,
                                             low_stock_threshold, archived, version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)""",
                    (product_id, f["category"], f["name"], f["description"], f["price"], f["color"], f["hex"],
                     json.dumps(f["sizes"]), f["cost_price"], f["low_stock_threshold"], f["archived"]))
        for wh in WAREHOUSES:
            cur.execute("INSERT INTO stock (product_id, warehouse, on_hand, version) VALUES (%s, %s, 0, 1)",
                        (product_id, wh))

    def update_product(self, product_id, fields):
        with self.tx() as cur:
            self._update_product(cur, product_id, fields)
            products, levels = self._catalog(cur, [product_id], include_archived=True)
        return {"products": self._with_stock(products, levels)}

    def _update_product(self, cur, product_id, fields):
        if not fields:
            raise ApiError(400, "Aucun champ à modifier")
        cols = ", ".join(f"{k} = %s" for k in fields)
        values = [json.dumps(v) if k == "sizes" else v for k, v in fields.items()]
        cur.execute(f"UPDATE products SET {cols}, version = version + 1 WHERE id = %s RETURNING id",
                    values + [product_id])
        if not cur.fetchone():
            raise ApiError(404, "Produit introuvable")

    def export_catalog(self):
        return self.list_products(include_archived=True)

    def import_catalog(self, rows):
        """Crée ou met à jour les produits du CSV ; stock_<entrepôt> fixe le stock physique
        par un mouvement d'inventaire. Tout ou rien (une transaction)."""
        created = updated = 0
        with self.tx() as cur:
            ids = [pid for pid, _, _ in rows]
            existing, _ = self._catalog(cur, ids, include_archived=True)
            for pid, fields, _ in rows:
                if pid in existing:
                    if fields:
                        self._update_product(cur, pid, fields)
                        updated += 1
                else:
                    _, full = domain.validate_product({**fields, "id": pid})
                    self._insert_product(cur, pid, full)
                    created += 1
            products, levels = self._catalog(cur, ids, include_archived=True, lock=True)
            before = self._totals(levels, ids)
            changed, movements = set(), []
            for pid, _, stock in rows:
                for wh, target in stock.items():
                    key = (pid, wh)
                    delta = target - levels[key]["on_hand"]
                    if delta == 0:
                        continue
                    if target < levels[key]["reserved"]:
                        raise ApiError(409, f"{pid} / {wh} : le stock importé est inférieur au stock réservé")
                    self._adjust(levels, key, on_hand=delta)
                    changed.add(key)
                    movements.append(domain.new_movement(pid, wh, "INVENTORY", delta, target, reason="import CSV"))
            stock = self._write_levels(cur, levels, changed)
            self._insert_movements(cur, movements)
            after = self._totals(levels, ids)
            alerts = domain.low_stock_crossings(products, before, after)
            products, levels = self._catalog(cur, ids, include_archived=True)
        return {"created": created, "updated": updated, "products": self._with_stock(products, levels),
                "stock": stock, "movements": movements, "alerts": alerts,
                "restocked": domain.restock_crossings(before, after)}

    # --- lot 2a : commandes d'un client, codes promo, avis, souhaits ----------

    def my_orders(self, user_id, limit=50):
        with self.tx() as cur:
            cur.execute("SELECT * FROM orders WHERE user_id = %s ORDER BY created_at DESC LIMIT %s", (user_id, limit))
            rows = [self._order(r) for r in cur.fetchall()]
        if self.status_store:  # hybride : le statut fait foi dans DynamoDB
            for r in rows:
                try:
                    r["status"] = self.status_store.get_order(r["order_id"])["status"]
                except ApiError:
                    pass
        return rows

    def list_promos(self):
        with self.tx() as cur:
            cur.execute("SELECT * FROM promos ORDER BY code")
            return [_promo(r) for r in cur.fetchall()]

    def save_promo(self, code, fields, create):
        with self.tx() as cur:
            if create:
                cur.execute("SELECT 1 FROM promos WHERE code = %s", (code,))
                if cur.fetchone():
                    raise ApiError(409, f"Le code {code} existe déjà")
                cur.execute("""INSERT INTO promos (code, type, value, min_total, max_uses, expires_at, active)
                               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                            (code, fields["type"], fields["value"], fields["min_total"], fields["max_uses"],
                             fields["expires_at"], fields["active"]))
            else:
                if not fields:
                    raise ApiError(400, "Aucun champ à modifier")
                cols = ", ".join(f"{k} = %s" for k in fields)
                cur.execute(f"UPDATE promos SET {cols}, version = version + 1 WHERE code = %s RETURNING code",
                            list(fields.values()) + [code])
                if not cur.fetchone():
                    raise ApiError(404, "Code promo introuvable")
            cur.execute("SELECT * FROM promos WHERE code = %s", (code,))
            return {"promos": [_promo(cur.fetchone())]}

    def list_reviews(self, product_id):
        with self.tx() as cur:
            cur.execute("SELECT * FROM reviews WHERE product_id = %s ORDER BY at DESC", (product_id,))
            return [dict(r) for r in cur.fetchall()]

    def _has_bought(self, cur, user_id, product_id):
        cur.execute("""SELECT order_id, status FROM orders
                       WHERE user_id = %s AND items @> %s::jsonb""",
                    (user_id, json.dumps([{"product_id": product_id}])))
        for row in cur.fetchall():
            status = row["status"]
            if self.status_store:  # hybride : le statut (annulation) fait foi dans DynamoDB
                try:
                    status = self.status_store.get_order(row["order_id"])["status"]
                except ApiError:
                    pass
            if status != "CANCELLED":
                return True
        return False

    def add_review(self, product_id, user, rating, comment):
        """Un avis par client et par produit (le dernier remplace le précédent), réservé
        aux clients qui ont commandé le produit. La note moyenne est tenue à jour dans la
        même transaction."""
        review = domain.new_review(product_id, user, rating, comment)
        with self.tx() as cur:
            cur.execute("SELECT 1 FROM products WHERE id = %s AND NOT archived FOR UPDATE", (product_id,))
            if not cur.fetchone():
                raise ApiError(404, "Produit introuvable")
            if not self._has_bought(cur, user["sub"], product_id):
                raise ApiError(403, "Seuls les clients ayant commandé ce produit peuvent laisser un avis")
            cur.execute("SELECT rating FROM reviews WHERE product_id = %s AND user_id = %s", (product_id, user["sub"]))
            old = cur.fetchone()
            cur.execute("""INSERT INTO reviews (product_id, user_id, author, rating, comment, at)
                           VALUES (%(product_id)s, %(user_id)s, %(author)s, %(rating)s, %(comment)s, %(at)s)
                           ON CONFLICT (product_id, user_id) DO UPDATE SET author = EXCLUDED.author,
                               rating = EXCLUDED.rating, comment = EXCLUDED.comment, at = EXCLUDED.at""", review)
            cur.execute("""UPDATE products SET rating_sum = rating_sum + %s, rating_count = rating_count + %s,
                                  version = version + 1 WHERE id = %s""",
                        (rating - (old["rating"] if old else 0), 0 if old else 1, product_id))
            products, levels = self._catalog(cur, [product_id], include_archived=True)
        return {"review": review, "reviews": [review], "products": self._with_stock(products, levels)}

    def wishlist(self, user_id):
        with self.tx() as cur:
            cur.execute("SELECT product_id FROM wishlists WHERE user_id = %s ORDER BY created_at", (user_id,))
            ids = [r["product_id"] for r in cur.fetchall()]
            products, levels = self._catalog(cur, ids)
        return self._with_stock(products, levels)

    def set_wish(self, user_id, product_id, wished, email=None):
        with self.tx() as cur:
            if wished:
                cur.execute("SELECT 1 FROM products WHERE id = %s AND NOT archived", (product_id,))
                if not cur.fetchone():
                    raise ApiError(404, "Produit introuvable")
                cur.execute("""INSERT INTO wishlists (user_id, product_id, created_at, email) VALUES (%s, %s, %s, %s)
                               ON CONFLICT (user_id, product_id) DO UPDATE SET email = EXCLUDED.email""",
                            (user_id, product_id, domain.now_iso(), email))
            else:
                cur.execute("DELETE FROM wishlists WHERE user_id = %s AND product_id = %s", (user_id, product_id))
        return {"wishes": [{"user_id": user_id, "product_id": product_id, "wished": wished, "email": email}]}

    def notifications(self, user_id, limit=50):
        with self.tx() as cur:
            cur.execute("SELECT * FROM notifications WHERE user_id = %s ORDER BY at DESC LIMIT %s", (user_id, limit))
            return [dict(r) for r in cur.fetchall()]

    def notify_back_in_stock(self, product_ids):
        """Notifie les clients qui ont le produit dans leur liste de souhaits."""
        if not product_ids:
            return []
        out = []
        with self.tx() as cur:
            products, _ = self._catalog(cur, product_ids)
            for pid, product in products.items():
                cur.execute("SELECT user_id, email FROM wishlists WHERE product_id = %s", (pid,))
                for row in cur.fetchall():
                    n = domain.new_notification(row["user_id"], product, row["email"])
                    cur.execute("""INSERT INTO notifications (notification_id, user_id, type, product_id, message, at)
                                   VALUES (%(notification_id)s, %(user_id)s, %(type)s, %(product_id)s,
                                           %(message)s, %(at)s)""", n)
                    out.append(n)
        return out

    # --- traitement interne (worker SQS de la VM) ------------------------------

    def advance(self, event):
        """Même contrat que DynamoStore.advance : (commande, événement suivant, rejoué) ou None."""
        transition = domain.TRANSITIONS.get(event["event_type"])
        if not transition:
            return None
        expected, new_status, next_event = transition
        order_id = event["order"]["order_id"]
        now = domain.now_iso()
        with self.tx() as cur:
            shipping = None
            if new_status == "SHIPPED":
                cur.execute("SELECT shipping_method FROM orders WHERE order_id = %s", (order_id,))
                row = cur.fetchone()
                shipping = domain.new_shipping(row["shipping_method"] if row else "standard")
            # Mise à jour conditionnelle => la transition n'est jamais appliquée deux fois.
            cur.execute(
                """UPDATE orders SET status = %s, updated_at = %s,
                          history = history || %s::jsonb, shipping = COALESCE(%s::jsonb, shipping)
                   WHERE order_id = %s AND status = %s RETURNING *""",
                (new_status, now, json.dumps([{"status": new_status, "at": now}]),
                 json.dumps(shipping) if shipping else None, order_id, expected))
            order = cur.fetchone()
            replayed = order is None
            if replayed:
                cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
                order = cur.fetchone()
        if not order or (replayed and order["status"] != new_status):
            return None
        return self._order(order), next_event, replayed


class _Transaction:
    """Context manager : une transaction sur une connexion du pool."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        if not self.store.slots.acquire(timeout=POOL_WAIT_SECONDS):
            raise ApiError(503, "Service surchargé, réessayez dans quelques instants")
        try:
            self.conn = self.store.pool.getconn()
        except Exception:
            self.store.slots.release()
            raise
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            self.store.pool.putconn(self.conn)
            self.store.slots.release()
