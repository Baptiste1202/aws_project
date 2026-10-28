"""Règles métier pures, sans accès aux données : partagées par toutes les solutions."""

import csv
import io
import json
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_LINES = 20
MAX_QTY = 10
MAX_BODY_BYTES = 64 * 1024
CARRIERS = ["Colissimo", "Chronopost", "DHL Express", "Mondial Relay"]

# Frais de port (lot 3) : mode -> (libellé, frais, gratuit à partir de, transporteurs).
SHIPPING_METHODS = {
    "standard": ("Livraison standard (3 à 5 jours)", Decimal("4.90"), Decimal("60"), ["Colissimo", "Mondial Relay"]),
    "express": ("Livraison express (24 h)", Decimal("9.90"), None, ["Chronopost", "DHL Express"]),
}

# Cycle de vie d'une commande :
#   PENDING_PAYMENT -> CONFIRMED (payée) -> PACKED -> SHIPPED -> IN_TRANSIT -> DELIVERED
#   (CANCELLED tant qu'elle n'est pas expédiée, ou si elle reste impayée)
#   DELIVERED -> RETURN_REQUESTED (client, 14 jours) -> RETURNED (réception, remboursement)
# event_type reçu -> (statut attendu, nouveau statut, événement publié). Ce sont les
# étapes du workflow Step Functions (serverless, hybride) et du worker EC2 (VM).
TRANSITIONS = {
    "ORDER_PAID": ("CONFIRMED", "PACKED", "ORDER_PACKED"),
    "ORDER_PACKED": ("PACKED", "SHIPPED", "ORDER_SHIPPED"),
    "ORDER_SHIPPED": ("SHIPPED", "IN_TRANSIT", "ORDER_IN_TRANSIT"),
    "ORDER_IN_TRANSIT": ("IN_TRANSIT", "DELIVERED", "ORDER_DELIVERED"),
}
PAYMENT_TTL = 900                 # 15 min pour payer, ensuite la purge relit Stripe
PAYMENT_TTL_RANGE = (10, 900)     # bornes acceptées (10 s : tests de la purge)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_json(obj):
    """default= de json.dumps : Decimal (DynamoDB, NUMERIC) et datetime."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    raise TypeError(f"Type non sérialisable : {type(obj)}")


def dumps(obj):
    return json.dumps(obj, default=to_json, ensure_ascii=False)


def new_order_id():
    return "ORD-" + uuid.uuid4().hex[:10].upper()


def parse_order(raw):
    """Valide le corps de POST /api/orders.

    Renvoie (client, lignes, quantités par produit) ; les lignes identiques
    (même produit, même taille) sont regroupées : une transaction ne touche
    qu'une fois chaque produit.
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise ApiError(400, "JSON invalide")
    customer = data.get("customer") or {}
    name = str(customer.get("name", "")).strip()
    email = str(customer.get("email", "")).strip()
    if not name or len(name) > 100:
        raise ApiError(400, "Nom du client invalide")
    if not EMAIL_RE.match(email):
        raise ApiError(400, "Adresse e-mail invalide")
    address = str(customer.get("address", "")).strip()
    if len(address) > 200:
        raise ApiError(400, "Adresse de livraison trop longue")
    lines = data.get("items") or []
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        raise ApiError(400, f"La commande doit contenir entre 1 et {MAX_LINES} articles")
    merged, per_product = {}, {}
    for line in lines:
        pid = str(line.get("product_id", ""))
        size = str(line.get("size", ""))
        try:
            qty = int(line.get("quantity", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "Quantité invalide")
        if not pid or not 1 <= qty <= MAX_QTY:
            raise ApiError(400, "Ligne de commande invalide")
        merged[(pid, size)] = merged.get((pid, size), 0) + qty
        per_product[pid] = per_product.get(pid, 0) + qty
    out = {"name": name, "email": email}
    if address:
        out["address"] = address
    return out, merged, per_product


def parse_checkout(data):
    """Options de POST /api/orders : mode de livraison et délai de paiement."""
    method = str(data.get("shipping_method") or "standard")
    if method not in SHIPPING_METHODS:
        raise ApiError(400, f"Mode de livraison inconnu (attendu : {', '.join(SHIPPING_METHODS)})")
    try:
        ttl = int(data.get("payment_ttl", PAYMENT_TTL))
    except (TypeError, ValueError):
        raise ApiError(400, "payment_ttl invalide")
    if not PAYMENT_TTL_RANGE[0] <= ttl <= PAYMENT_TTL_RANGE[1]:
        raise ApiError(400, f"payment_ttl doit être entre {PAYMENT_TTL_RANGE[0]} et {PAYMENT_TTL_RANGE[1]} s")
    return method, ttl


def shipping_fee(method, merchandise):
    """Frais de port selon le mode et le montant des articles (après remise)."""
    _, fee, free_from, _ = SHIPPING_METHODS[method]
    return Decimal("0.00") if free_from is not None and merchandise >= free_from else fee


def price_lines(lines, catalog):
    """Lignes de commande et total, à partir du catalogue {id: produit}.

    Un produit a les clés "name", "price" (Decimal) et "sizes" (liste).
    """
    items, total = [], Decimal("0")
    for (pid, size), qty in lines.items():
        product = catalog.get(pid)
        if not product:
            raise ApiError(400, f"Produit inconnu : {pid}")
        if size not in product["sizes"]:
            raise ApiError(400, f"Taille {size} indisponible pour {product['name']}")
        price = Decimal(str(product["price"]))
        total += price * qty
        items.append({"product_id": pid, "name": product["name"], "size": size,
                      "quantity": qty, "unit_price": price})
    return items, total


def new_order(order_id, customer, items, total, backend):
    created_at = now_iso()
    return {"order_id": order_id, "status": "PENDING_PAYMENT", "customer": customer, "items": items,
            "total": total, "created_at": created_at, "updated_at": created_at,
            "history": [{"status": "PENDING_PAYMENT", "at": created_at}], "backend": backend}


def payment_deadline(ttl):
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(timespec="seconds")


def new_shipping(method="standard"):
    """Transporteur simulé (selon le mode de livraison) et numéro de suivi."""
    carriers = SHIPPING_METHODS.get(method, SHIPPING_METHODS["standard"])[3]
    return {"carrier": random.choice(carriers), "tracking_number": "FR" + uuid.uuid4().hex[:12].upper()}


def public_order(order):
    """Commande renvoyée par l'API : jamais le client_secret Stripe."""
    out = dict(order)
    if out.get("payment"):
        out["payment"] = {k: v for k, v in out["payment"].items() if k != "client_secret"}
    return out


# ---------------------------------------------------------------------------
# Stock : entrepôts, allocation, réservations, alertes
# ---------------------------------------------------------------------------

# Ordre de priorité : une commande est servie d'abord depuis Paris, puis Lyon.
WAREHOUSES = ["PAR", "LYS"]
WAREHOUSE_NAMES = {"PAR": "Paris", "LYS": "Lyon"}
CANCELLABLE = ("PENDING_PAYMENT", "CONFIRMED", "PACKED")
PAID_STATUSES = ("CONFIRMED", "PACKED", "SHIPPED", "IN_TRANSIT", "DELIVERED", "RETURN_REQUESTED", "RETURNED")
MOVEMENT_TYPES = ("SALE", "CANCEL", "RESTOCK", "BREAKAGE", "INVENTORY", "RETURN")
ADMIN_MOVEMENTS = ("RESTOCK", "BREAKAGE", "INVENTORY")
RESERVATION_TTL = 900               # 15 min par défaut
RESERVATION_TTL_RANGE = (10, 900)   # bornes acceptées (10 s : tests d'expiration)
DEFAULT_LOW_STOCK = 5
# Deux lettres et 3 à 9 chiffres (catalogue : TS-001 ; tests : suffixe aléatoire unique).
PRODUCT_ID_RE = re.compile(r"^[A-Z]{2}-\d{3,9}$")
CATEGORIES = ("tshirt", "pull", "baskets")


# ---------------------------------------------------------------------------
# Retours (lot 3b) : droit de rétractation de 14 jours après la livraison. La commande
# entière est retournée et remboursée, frais de livraison compris (comme l'impose le
# droit de rétractation pour la livraison standard).
# ---------------------------------------------------------------------------

RETURN_WINDOW_DAYS = 14
RETURN_REASONS = ("size", "changed_mind", "defective", "other")


def delivered_at(order):
    return next((h["at"] for h in reversed(order.get("history") or []) if h["status"] == "DELIVERED"), None)


def return_deadline(order):
    at = delivered_at(order)
    return (datetime.fromisoformat(at) + timedelta(days=RETURN_WINDOW_DAYS)).isoformat(timespec="seconds") if at else None


def check_returnable(order, user, now=None):
    """Le client de la commande (ou un admin) peut demander le retour d'une commande
    livrée depuis moins de 14 jours."""
    if not user or (order.get("user_id") != user["sub"] and "admin" not in user["groups"]):
        raise ApiError(403, "Retour réservé au client de la commande (connecté)")
    if order["status"] != "DELIVERED":
        raise ApiError(409, f"Retour impossible pour une commande {order['status']} (livrée seulement)")
    if (now or now_iso()) > return_deadline(order):
        raise ApiError(409, f"Délai de retour dépassé ({RETURN_WINDOW_DAYS} jours après la livraison)")


def parse_return(raw):
    data = _json_body(raw)
    reason = str(data.get("reason") or "other")
    if reason not in RETURN_REASONS:
        raise ApiError(400, f"Motif invalide (attendu : {', '.join(RETURN_REASONS)})")
    return {"reason": reason, "comment": str(data.get("comment") or "")[:500], "requested_at": now_iso(),
            "label": "RT" + uuid.uuid4().hex[:12].upper()}  # étiquette retour (simulée)


# ---------------------------------------------------------------------------
# Réapprovisionnement fournisseur (lot 3b) : une alerte de stock bas crée un bon de
# commande (un seul ouvert par produit), livré après un délai simulé à Paris.
# ---------------------------------------------------------------------------

SUPPLIERS = {"tshirt": "Atelier Textile de Roubaix", "pull": "Atelier Textile de Roubaix",
             "baskets": "Sneakers Factory Porto"}
RESTOCK_WAREHOUSE = "PAR"


def reorder_quantity(product):
    """Quantité commandée : de quoi remonter nettement au-dessus du seuil."""
    return max(10, 4 * int(product.get("low_stock_threshold", DEFAULT_LOW_STOCK)))


def new_purchase_order(product, alert=None):
    now = now_iso()
    return {"po_id": "PO-" + uuid.uuid4().hex[:10].upper(), "product_id": product["id"], "name": product["name"],
            "supplier": SUPPLIERS.get(product.get("category"), "Fournisseur Threadly"),
            "warehouse": RESTOCK_WAREHOUSE, "quantity": reorder_quantity(product), "status": "OPEN",
            "created_at": now, "updated_at": now, "received_at": None,
            "alert": {"available": alert["available"], "threshold": alert["threshold"]} if alert else None,
            "version": 1}


def initial_split(stock):
    """Répartition du stock initial de data/products.json : 2/3 Paris, 1/3 Lyon."""
    lyon = int(stock) // 3
    return {"PAR": int(stock) - lyon, "LYS": lyon}


def default_cost(price):
    """Prix de revient par défaut (45 % du prix de vente), pour la valorisation."""
    return (Decimal(str(price)) * Decimal("0.45")).quantize(Decimal("0.01"), ROUND_HALF_UP)


def seed_product(p):
    """Produit de data/products.json complété des champs du lot 1."""
    return {"id": p["id"], "category": p["category"], "name": p["name"], "description": p["description"],
            "price": Decimal(str(p["price"])), "color": p["color"], "hex": p["hex"], "sizes": list(p["sizes"]),
            "cost_price": Decimal(str(p.get("cost_price") or default_cost(p["price"]))),
            "low_stock_threshold": int(p.get("low_stock_threshold", DEFAULT_LOW_STOCK)),
            "archived": bool(p.get("archived", False))}


def level(product_id, warehouse, on_hand, reserved, version):
    """Niveau de stock d'un produit dans un entrepôt (forme des événements et de l'API)."""
    on_hand, reserved = int(on_hand), int(reserved)
    return {"product_id": product_id, "warehouse": warehouse, "on_hand": on_hand,
            "reserved": reserved, "available": on_hand - reserved, "version": int(version)}


def with_stock(product, levels):
    """Ajoute au produit son stock : "stock" = disponible total (API historique) et le
    détail par entrepôt."""
    by_wh = {lv["warehouse"]: {"on_hand": lv["on_hand"], "reserved": lv["reserved"],
                               "available": lv["available"]} for lv in levels}
    for wh in WAREHOUSES:
        by_wh.setdefault(wh, {"on_hand": 0, "reserved": 0, "available": 0})
    return {**product, "stock": sum(v["available"] for v in by_wh.values()), "stock_by_warehouse": by_wh}


def allocate(needed, available, held=None):
    """Répartit les quantités commandées entre les entrepôts.

    needed    : {produit: quantité}
    available : {(produit, entrepôt): disponible hors réservations}
    held      : {(produit, entrepôt): quantité réservée par CE panier}
    Renvoie [{product_id, warehouse, quantity, from_reservation}] ; les réservations du
    panier passent en premier, puis Paris, puis Lyon. Lève 409 si le stock manque.
    """
    held = held or {}
    allocations, short = [], []
    for pid, qty in needed.items():
        left = qty
        for wh in WAREHOUSES:
            use = min(held.get((pid, wh), 0), left)
            if use:
                allocations.append({"product_id": pid, "warehouse": wh, "quantity": use, "from_reservation": use})
                left -= use
        for wh in WAREHOUSES:
            use = min(max(available.get((pid, wh), 0), 0), left)
            if use:
                merged = next((a for a in allocations if a["product_id"] == pid and a["warehouse"] == wh), None)
                if merged:
                    merged["quantity"] += use
                else:
                    allocations.append({"product_id": pid, "warehouse": wh, "quantity": use, "from_reservation": 0})
                left -= use
        if left:
            short.append(pid)
    if short:
        raise ApiError(409, "Stock insuffisant pour : " + ", ".join(short))
    return allocations


def reservation_ttl(value):
    if value in (None, ""):
        return RESERVATION_TTL
    try:
        ttl = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Durée de réservation invalide")
    low, high = RESERVATION_TTL_RANGE
    if not low <= ttl <= high:
        raise ApiError(400, f"La durée de réservation doit être comprise entre {low} et {high} s")
    return ttl


def expires_at(ttl):
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(timespec="seconds")


def check_cart_id(cart_id):
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", cart_id or ""):
        raise ApiError(400, "Identifiant de panier invalide")
    return cart_id


def parse_reservation(cart_id, raw):
    check_cart_id(cart_id)
    data = _json_body(raw)
    pid = str(data.get("product_id", ""))
    try:
        qty = int(data.get("quantity", 0))
    except (TypeError, ValueError):
        raise ApiError(400, "Quantité invalide")
    if not pid or not 0 <= qty <= MAX_QTY * MAX_LINES:
        raise ApiError(400, "Réservation invalide")
    return pid, qty, reservation_ttl(data.get("ttl_seconds"))


def low_stock_crossings(products, before, after):
    """Produits dont le disponible total vient de passer sous leur seuil.

    products : {id: produit} ; before / after : {id: disponible total}.
    """
    alerts = []
    for pid, now in after.items():
        threshold = int(products[pid].get("low_stock_threshold", DEFAULT_LOW_STOCK))
        if before.get(pid, now) >= threshold > now:
            alerts.append({"product_id": pid, "name": products[pid]["name"], "available": now,
                           "threshold": threshold})
    return alerts


def parse_movement(raw):
    data = _json_body(raw)
    pid, wh, kind = str(data.get("product_id", "")), str(data.get("warehouse", "")), str(data.get("type", ""))
    if wh not in WAREHOUSES:
        raise ApiError(400, f"Entrepôt inconnu (attendu : {', '.join(WAREHOUSES)})")
    if kind not in ADMIN_MOVEMENTS:
        raise ApiError(400, f"Type de mouvement invalide (attendu : {', '.join(ADMIN_MOVEMENTS)})")
    try:
        qty = int(data.get("quantity"))
    except (TypeError, ValueError):
        raise ApiError(400, "Quantité invalide")
    if qty < 0 or (kind != "INVENTORY" and qty == 0) or qty > 100000:
        raise ApiError(400, "Quantité invalide")
    return pid, wh, kind, qty, str(data.get("reason", ""))[:200]


def movement_delta(kind, qty, on_hand):
    """Variation du stock physique pour un mouvement d'admin (inventaire : nouvelle valeur)."""
    if kind == "RESTOCK":
        return qty
    if kind == "BREAKAGE":
        return -qty
    return qty - on_hand  # INVENTORY


def new_movement(product_id, warehouse, kind, delta, on_hand_after, ref="", reason=""):
    # Horodatage à la microseconde : le journal est trié par date, et plusieurs
    # mouvements arrivent souvent dans la même seconde.
    at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return {"movement_id": uuid.uuid4().hex, "product_id": product_id, "warehouse": warehouse, "type": kind,
            "delta": int(delta), "on_hand_after": int(on_hand_after), "ref": ref, "reason": reason, "at": at}


def valuation(products, levels):
    """Valeur du stock physique (on_hand × prix de revient), par produit et par entrepôt."""
    rows, by_wh = [], {wh: {"units": 0, "value": Decimal("0")} for wh in WAREHOUSES}
    for lv in levels:
        product = products.get(lv["product_id"])
        if not product:
            continue
        value = Decimal(str(product["cost_price"])) * lv["on_hand"]
        rows.append({"product_id": lv["product_id"], "name": product["name"], "warehouse": lv["warehouse"],
                     "on_hand": lv["on_hand"], "cost_price": product["cost_price"], "value": value})
        by_wh[lv["warehouse"]]["units"] += lv["on_hand"]
        by_wh[lv["warehouse"]]["value"] += value
    rows.sort(key=lambda r: (r["product_id"], WAREHOUSES.index(r["warehouse"])))
    return {"warehouses": by_wh, "lines": rows,
            "total_units": sum(v["units"] for v in by_wh.values()),
            "total_value": sum((v["value"] for v in by_wh.values()), Decimal("0"))}


# ---------------------------------------------------------------------------
# Produits (admin) et CSV du catalogue
# ---------------------------------------------------------------------------

PRODUCT_FIELDS = ("category", "name", "description", "price", "color", "hex", "sizes",
                  "cost_price", "low_stock_threshold", "archived")
CSV_COLUMNS = ["id", "category", "name", "description", "price", "cost_price", "color", "hex", "sizes",
               "low_stock_threshold", "archived"] + [f"stock_{wh}" for wh in WAREHOUSES]


def _json_body(raw):
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise ApiError(400, "JSON invalide")
    if not isinstance(data, dict):
        raise ApiError(400, "JSON invalide")
    return data


def validate_product(data, partial=False, product_id=None):
    """Champs d'un produit créé (tous obligatoires) ou modifié (partial=True)."""
    out = {}
    pid = product_id or str(data.get("id", ""))
    if not PRODUCT_ID_RE.match(pid):
        raise ApiError(400, "Identifiant produit invalide (format AA-000)")
    required = () if partial else ("category", "name", "description", "price", "color", "hex", "sizes")
    for f in required:
        if data.get(f) in (None, "", []):
            raise ApiError(400, f"Champ obligatoire manquant : {f}")
    for f in PRODUCT_FIELDS:
        if f not in data:
            continue
        v = data[f]
        if f == "category" and v not in CATEGORIES:
            raise ApiError(400, f"Catégorie invalide (attendu : {', '.join(CATEGORIES)})")
        if f in ("price", "cost_price"):
            try:
                v = Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_UP)
            except Exception:  # noqa: BLE001
                raise ApiError(400, f"Montant invalide : {f}")
            if v < 0 or v > 100000:
                raise ApiError(400, f"Montant invalide : {f}")
        if f == "sizes":
            if isinstance(v, str):
                v = [s for s in v.split("|") if s]
            if not isinstance(v, list) or not v or not all(isinstance(s, str) and s for s in v):
                raise ApiError(400, "Tailles invalides")
        if f == "low_stock_threshold":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ApiError(400, "Seuil invalide")
            if v < 0:
                raise ApiError(400, "Seuil invalide")
        if f == "archived":
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "oui", "yes")
        if f in ("name", "description", "color", "hex"):
            v = str(v).strip()[:500]
        out[f] = v
    if not partial:
        out.setdefault("cost_price", default_cost(out["price"]))
        out.setdefault("low_stock_threshold", DEFAULT_LOW_STOCK)
        out.setdefault("archived", False)
    return pid, out


def catalog_to_csv(products):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for p in products:
        row = {c: p.get(c, "") for c in CSV_COLUMNS}
        row["sizes"] = "|".join(p["sizes"])
        row["archived"] = "true" if p.get("archived") else "false"
        for wh in WAREHOUSES:
            row[f"stock_{wh}"] = p["stock_by_warehouse"][wh]["on_hand"]
        writer.writerow({k: to_json(v) if isinstance(v, Decimal) else v for k, v in row.items()})
    return buf.getvalue()


def csv_to_catalog(text):
    """Lignes du CSV -> [(id, champs, {entrepôt: stock physique ou None})]."""
    reader = csv.DictReader(io.StringIO(text))
    missing = {"id"} - set(reader.fieldnames or [])
    if missing:
        raise ApiError(400, "Colonne obligatoire manquante : id")
    rows = []
    for n, row in enumerate(reader, start=2):
        data = {k: v for k, v in row.items() if k in PRODUCT_FIELDS and v not in (None, "")}
        try:
            pid, fields = validate_product(data, partial=True, product_id=(row.get("id") or "").strip())
        except ApiError as e:
            raise ApiError(400, f"Ligne {n} : {e.message}")
        stock = {}
        for wh in WAREHOUSES:
            raw = (row.get(f"stock_{wh}") or "").strip()
            if raw:
                if not raw.isdigit():
                    raise ApiError(400, f"Ligne {n} : stock_{wh} invalide")
                stock[wh] = int(raw)
        rows.append((pid, fields, stock))
    if not rows:
        raise ApiError(400, "Fichier CSV vide")
    return rows


# ---------------------------------------------------------------------------
# Lot 2a : recherche, codes promo, avis, retour en stock
# ---------------------------------------------------------------------------

SORTS = {"price": lambda p: (p["price"], p["id"]), "-price": lambda p: (-p["price"], p["id"]),
         "name": lambda p: (p["name"].casefold(), p["id"]),
         "rating": lambda p: (-(p.get("rating_avg") or 0), p["id"])}


def _money(value, field):
    try:
        v = Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        raise ApiError(400, f"Paramètre {field} invalide")
    if v < 0:
        raise ApiError(400, f"Paramètre {field} invalide")
    return v


def search_products(products, query):
    """Recherche et filtres du catalogue : q (texte), category, min_price, max_price,
    in_stock=1, sort=price|-price|name|rating. Le catalogue (quelques dizaines de
    produits) est filtré par l'application ; au-delà, un moteur de recherche
    (OpenSearch) s'imposerait."""
    q = (query or {}).get("q", "").strip().casefold()
    category = (query or {}).get("category")
    low = _money(query["min_price"], "min_price") if (query or {}).get("min_price") else None
    high = _money(query["max_price"], "max_price") if (query or {}).get("max_price") else None
    sort = (query or {}).get("sort") or "id"
    if sort != "id" and sort not in SORTS:
        raise ApiError(400, f"Tri invalide (attendu : {', '.join(SORTS)})")
    out = []
    for p in products:
        text = " ".join(str(p.get(f, "")) for f in ("name", "description", "color", "category")).casefold()
        if q and not all(word in text for word in q.split()):
            continue
        if category and p["category"] != category:
            continue
        price = Decimal(str(p["price"]))
        if (low is not None and price < low) or (high is not None and price > high):
            continue
        if (query or {}).get("in_stock") in ("1", "true") and p["stock"] <= 0:
            continue
        out.append(p)
    return sorted(out, key=SORTS[sort]) if sort in SORTS else out


PROMO_CODE_RE = re.compile(r"^[A-Z0-9]{3,20}$")


def validate_promo(data, partial=False, code=None):
    code = (code or str(data.get("code", ""))).strip().upper()
    if not PROMO_CODE_RE.match(code):
        raise ApiError(400, "Code promo invalide (3 à 20 lettres ou chiffres)")
    out = {}
    if not partial or "type" in data:
        if data.get("type") not in ("percent", "fixed"):
            raise ApiError(400, "Type de promotion invalide (percent ou fixed)")
        out["type"] = data["type"]
    if not partial or "value" in data:
        out["value"] = _money(data.get("value"), "value")
        if out["value"] <= 0 or (data.get("type") == "percent" and out["value"] > 90):
            raise ApiError(400, "Valeur de promotion invalide (pourcentage : 90 au plus)")
    for f in ("min_total",):
        if f in data or not partial:
            out[f] = _money(data.get(f) or 0, f)
    if "max_uses" in data or not partial:
        mu = data.get("max_uses")
        if mu not in (None, ""):
            try:
                mu = int(mu)
            except (TypeError, ValueError):
                raise ApiError(400, "max_uses invalide")
            if mu < 1:
                raise ApiError(400, "max_uses invalide")
        out["max_uses"] = mu if mu not in ("",) else None
    if "expires_at" in data or not partial:
        exp = data.get("expires_at") or None
        if exp:
            try:
                datetime.fromisoformat(str(exp))
            except ValueError:
                raise ApiError(400, "expires_at invalide (date ISO 8601)")
        out["expires_at"] = exp
    if "active" in data or not partial:
        out["active"] = data.get("active", True) is True or str(data.get("active")).lower() == "true"
    return code, out


def promo_discount(promo, subtotal):
    """Remise d'un code promo sur le sous-total. Lève 409 si le code n'est pas utilisable."""
    if not promo or not promo.get("active"):
        raise ApiError(409, "Code promo inconnu ou désactivé")
    if promo.get("expires_at"):
        expires = datetime.fromisoformat(promo["expires_at"])
        if expires.tzinfo is None:  # date sans fuseau : UTC
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise ApiError(409, "Code promo expiré")
    if promo.get("max_uses") is not None and int(promo.get("uses", 0)) >= int(promo["max_uses"]):
        raise ApiError(409, "Code promo épuisé")
    if subtotal < Decimal(str(promo.get("min_total") or 0)):
        raise ApiError(409, f"Code promo valable à partir de {to_json(Decimal(str(promo['min_total'])))} €")
    value = Decimal(str(promo["value"]))
    discount = subtotal * value / 100 if promo["type"] == "percent" else value
    return min(discount, subtotal).quantize(Decimal("0.01"), ROUND_HALF_UP)


def apply_totals(order, discount, promo_code, shipping_method="standard"):
    """Total = articles - remise + frais de port (gratuits au-delà du seuil, après remise)."""
    order["subtotal"] = order["total"]
    order["discount"] = discount
    order["promo_code"] = promo_code
    merchandise = order["subtotal"] - discount
    order["shipping_method"] = shipping_method
    order["shipping_fee"] = shipping_fee(shipping_method, merchandise)
    order["total"] = merchandise + order["shipping_fee"]
    return order


def validate_review(raw):
    data = _json_body(raw)
    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        raise ApiError(400, "Note invalide (1 à 5)")
    if not 1 <= rating <= 5:
        raise ApiError(400, "Note invalide (1 à 5)")
    comment = str(data.get("comment", "")).strip()[:1000]
    return rating, comment


def new_review(product_id, user, rating, comment):
    return {"product_id": product_id, "user_id": user["sub"], "author": user["name"][:60], "rating": rating,
            "comment": comment, "at": now_iso()}


def with_rating(product):
    count = int(product.get("rating_count") or 0)
    total = Decimal(str(product.get("rating_sum") or 0))
    return {**product, "rating_count": count,
            "rating_avg": (total / count).quantize(Decimal("0.1"), ROUND_HALF_UP) if count else None}


def restock_crossings(before, after):
    """Produits dont le disponible repasse au-dessus de 0 (alerte de retour en stock)."""
    return [pid for pid, now in after.items() if before.get(pid, now) <= 0 < now]


def new_notification(user_id, product, email=None):
    """Alerte de retour en stock : dans le compte du client, et par e-mail (Lambda mailer)
    si son adresse a été enregistrée avec le souhait."""
    n = {"notification_id": uuid.uuid4().hex, "user_id": user_id, "type": "BACK_IN_STOCK",
         "product_id": product["id"], "name": product["name"],
         "message": f"{product['name']} est de nouveau disponible.",
         "at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
    if email:
        n["email"] = email
    return n


# ---------------------------------------------------------------------------
# Pilotage (lot 4) : tableau de bord du back-office, rapport quotidien CSV
# ---------------------------------------------------------------------------

DASHBOARD_DAYS = 7


def counts_as_sale(order):
    """Commande payée et non remboursée (une commande retournée sort du chiffre d'affaires)."""
    return order["status"] in PAID_STATUSES and (order.get("payment") or {}).get("status") != "refunded"


def _since(days=0, now=None):
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    return start.isoformat(timespec="seconds")


def dashboard(orders, products, now=None):
    """Chiffre d'affaires du jour et des 7 derniers jours, top ventes, ruptures et
    stocks bas, commandes par statut. orders : commandes créées depuis week_start()."""
    today, week = _since(0, now), _since(DASHBOARD_DAYS - 1, now)
    sales = [o for o in orders if o["created_at"] >= week and counts_as_sale(o)]
    sold = {}
    for o in sales:
        for i in o["items"]:
            row = sold.setdefault(i["product_id"], {"product_id": i["product_id"], "name": i["name"],
                                                    "quantity": 0, "revenue": Decimal("0")})
            row["quantity"] += int(i["quantity"])
            row["revenue"] += Decimal(str(i["unit_price"])) * int(i["quantity"])
    statuses = {}
    for o in orders:
        if o["created_at"] >= week:
            statuses[o["status"]] = statuses.get(o["status"], 0) + 1
    money = lambda xs: sum((Decimal(str(o["total"])) for o in xs), Decimal("0"))  # noqa: E731
    day_sales = [o for o in sales if o["created_at"] >= today]
    active = [p for p in products if not p.get("archived")]
    return {
        "since": {"day": today, "week": week},
        "revenue": {"day": money(day_sales), "week": money(sales)},
        "orders": {"day": len(day_sales), "week": len(sales)},
        "top_products": sorted(sold.values(), key=lambda r: (-r["quantity"], r["product_id"]))[:5],
        "out_of_stock": [{"id": p["id"], "name": p["name"]} for p in active if p["stock"] <= 0],
        "low_stock": [{"id": p["id"], "name": p["name"], "stock": p["stock"], "threshold": p["low_stock_threshold"]}
                      for p in active if 0 < p["stock"] < int(p["low_stock_threshold"])],
        "orders_by_status": dict(sorted(statuses.items())),
    }


def week_start(now=None):
    return _since(DASHBOARD_DAYS - 1, now)


REPORT_COLUMNS = ["product_id", "name", "category", "sold_24h", "revenue_24h", "stock_PAR", "stock_LYS",
                  "reserved", "available", "low_stock_threshold", "cost_price", "stock_value"]


def daily_report(orders, products, valuation_result, backend, now=None):
    """Rapport quotidien : ventes des dernières 24 h, stock et valorisation par produit,
    puis une ligne TOTAL. Renvoie (nom du fichier, texte CSV, résumé)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=1)).isoformat(timespec="seconds")
    sold = {}
    for o in orders:
        if o["created_at"] >= since and counts_as_sale(o):
            for i in o["items"]:
                q, r = sold.get(i["product_id"], (0, Decimal("0")))
                sold[i["product_id"]] = (q + int(i["quantity"]), r + Decimal(str(i["unit_price"])) * int(i["quantity"]))
    values = {}
    for line in valuation_result["lines"]:
        values[line["product_id"]] = values.get(line["product_id"], Decimal("0")) + Decimal(str(line["value"]))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(REPORT_COLUMNS)
    total = {"sold": 0, "revenue": Decimal("0"), "value": Decimal("0")}
    for p in sorted(products, key=lambda p: p["id"]):
        by = p["stock_by_warehouse"]
        q, r = sold.get(p["id"], (0, Decimal("0")))
        value = values.get(p["id"], Decimal("0"))
        total["sold"] += q
        total["revenue"] += r
        total["value"] += value
        writer.writerow([p["id"], p["name"], p["category"], q, f"{r:.2f}", by["PAR"]["on_hand"], by["LYS"]["on_hand"],
                         sum(int(v["reserved"]) for v in by.values()), p["stock"], p["low_stock_threshold"],
                         f"{Decimal(str(p['cost_price'])):.2f}", f"{value:.2f}"])
    writer.writerow(["TOTAL", "", "", total["sold"], f"{total['revenue']:.2f}", "", "", "", "", "", "",
                     f"{total['value']:.2f}"])
    name = f"rapport-{backend}-{now:%Y-%m-%dT%H%M%S}Z.csv"
    summary = {"generated_at": now.isoformat(timespec="seconds"), "backend": backend, "products": len(products),
               "sold_24h": total["sold"], "revenue_24h": total["revenue"], "stock_value": total["value"]}
    return name, buf.getvalue(), summary


# ---------------------------------------------------------------------------
# Événements SNS
# ---------------------------------------------------------------------------

def publish(sns, topic_arn, event_type, order, source, stock=None):
    """Publie un événement de commande (voir publish_event)."""
    return publish_event(sns, topic_arn, event_type, source, order=order, stock=stock)


def publish_event(sns, topic_arn, event_type, source, order=None, **payload):
    """Publie un événement sur SNS (attributs source et event_type pour les filtres des
    abonnements). Les e-mails aux clients partent par SES (Lambda mailer, shop/mail.py)."""
    event = {"event_id": str(uuid.uuid4()), "event_type": event_type, "occurred_at": now_iso(),
             "source": source}
    if order is not None:
        event["order"] = order
    event.update({k: v for k, v in payload.items() if v is not None})
    message = {"default": json.dumps(event, default=to_json)}
    subject = f"Threadly : {event_type}" + (f" {order['order_id']}" if order is not None else "")
    sns.publish(
        TopicArn=topic_arn, MessageStructure="json", Message=json.dumps(message), Subject=subject,
        MessageAttributes={
            "event_type": {"DataType": "String", "StringValue": event_type},
            "source": {"DataType": "String", "StringValue": source},
        })
    return event
