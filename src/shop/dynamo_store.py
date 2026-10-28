"""Accès aux données DynamoDB : solution serverless et modèle de lecture de l'hybride.

Une seule table par solution (« single-table design »), clés pk / sk, et un index
secondaire gsi1 (gsi1pk = type, gsi1sk = tri) pour lister chaque type :

  PRODUCT#<id>  META            produit (prix, coût, seuil, archivé, version)
  PRODUCT#<id>  STOCK#<entrepôt> on_hand, reserved, available, version
  ORDER#<id>    META            commande (statut, historique, répartition)
  ORDER#<id>    PENDING         marqueur « impayée » (gsi1 PENDINGPAY, trié par délai)
  ORDER#<id>    RETURN          marqueur « retour demandé » (gsi1 RETURNREQ, trié par date)
  MOVE#<id>     <date>#<id>     mouvement de stock (journal)
  PO#<id>       META            bon de commande fournisseur (gsi1 PO, trié par date)
  SUPPLY#<id>   OPEN            bon ouvert d'un produit : un seul à la fois
  CART#<id>     RESV#<produit>  réservation (allocations, expires_at, ttl)

Le disponible (available = on_hand - reserved) est stocké : une condition DynamoDB
ne sait pas soustraire deux attributs, mais sait vérifier available >= q. Les
écritures de stock sont des transactions conditionnelles, réessayées sur conflit.
"""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from shop import domain
from shop.domain import ApiError, WAREHOUSES

RETRIES = 5
# "entity" : type d'élément (PRODUCT, STOCK...) ; pas "type", qui est un champ des mouvements.
KEYS = ("pk", "sk", "gsi1pk", "gsi1sk", "ttl", "entity")
_ser, _deser = TypeSerializer(), TypeDeserializer()


def to_item(obj):
    """Objet JSON -> élément DynamoDB (les float deviennent des Decimal)."""
    return json.loads(json.dumps(obj, default=domain.to_json), parse_float=Decimal)


def _clean(item):
    return {k: v for k, v in item.items() if k not in KEYS}


def _conditional_failed(e):
    return e.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _av(value):
    return _ser.serialize(to_item(value) if isinstance(value, (dict, list, float)) else value)


def product_keys(pid):
    return {"pk": f"PRODUCT#{pid}", "sk": "META", "gsi1pk": "PRODUCT", "gsi1sk": pid, "entity": "PRODUCT"}


def stock_keys(pid, wh):
    return {"pk": f"PRODUCT#{pid}", "sk": f"STOCK#{wh}", "gsi1pk": "STOCK", "gsi1sk": f"{pid}#{wh}", "entity": "STOCK"}


def order_keys(order):
    return {"pk": f"ORDER#{order['order_id']}", "sk": "META", "gsi1pk": "ORDER",
            "gsi1sk": f"{order['created_at']}#{order['order_id']}", "entity": "ORDER"}


def pending_keys(order):
    """Marqueur d'une commande impayée : la purge liste les délais dépassés par gsi1."""
    return {"pk": f"ORDER#{order['order_id']}", "sk": "PENDING", "gsi1pk": "PENDINGPAY",
            "gsi1sk": f"{order['payment_deadline']}#{order['order_id']}", "entity": "PENDING",
            "order_id": order["order_id"]}


def return_keys(order_id, requested_at):
    """Marqueur d'un retour à réceptionner : le magasinier liste les retours par gsi1."""
    return {"pk": f"ORDER#{order_id}", "sk": "RETURN", "gsi1pk": "RETURNREQ",
            "gsi1sk": f"{requested_at}#{order_id}", "entity": "RETURN", "order_id": order_id}


def po_keys(po):
    return {"pk": f"PO#{po['po_id']}", "sk": "META", "gsi1pk": "PO", "gsi1sk": f"{po['created_at']}#{po['po_id']}",
            "entity": "PO"}


def move_keys(m):
    return {"pk": f"MOVE#{m['product_id']}", "sk": f"{m['at']}#{m['movement_id']}", "gsi1pk": "MOVE",
            "gsi1sk": f"{m['at']}#{m['movement_id']}", "entity": "MOVE"}


def promo_keys(code):
    return {"pk": f"PROMO#{code}", "sk": "META", "gsi1pk": "PROMO", "gsi1sk": code, "entity": "PROMO"}


def review_keys(pid, user_id):
    return {"pk": f"PRODUCT#{pid}", "sk": f"REVIEW#{user_id}", "entity": "REVIEW"}


def user_order_keys(user_id, order):
    return {"pk": f"USER#{user_id}", "sk": f"ORDER#{order['created_at']}#{order['order_id']}", "entity": "USER_ORDER"}


def wish_keys(user_id, pid):
    # gsi1 : les clients qui souhaitent un produit (alerte de retour en stock).
    return {"pk": f"USER#{user_id}", "sk": f"WISH#{pid}", "gsi1pk": f"WISH#{pid}", "gsi1sk": user_id, "entity": "WISH"}


def notif_keys(n):
    return {"pk": f"USER#{n['user_id']}", "sk": f"NOTIF#{n['at']}#{n['notification_id']}", "entity": "NOTIF"}


def resv_keys(cart_id, pid, expires):
    return {"pk": f"CART#{cart_id}", "sk": f"RESV#{pid}", "gsi1pk": "RESV", "gsi1sk": expires, "entity": "RESV"}


def seed_items(raw_products):
    """Éléments initiaux (produits et stock par entrepôt) : Terraform et la stack locale."""
    items = []
    for raw in raw_products:
        p = domain.seed_product(raw)
        items.append(to_item({**product_keys(p["id"]), **p, "version": 0}))
        for wh, qty in domain.initial_split(raw["stock"]).items():
            items.append({**stock_keys(p["id"], wh), "product_id": p["id"], "warehouse": wh, "on_hand": qty,
                          "reserved": 0, "available": qty, "version": 0})
    return items


class DynamoStore:
    def __init__(self, table, backend="serverless"):
        self.name = table
        self.table = boto3.resource("dynamodb").Table(table)
        self.client = boto3.client("dynamodb")
        self.backend = backend

    # --- lectures -----------------------------------------------------------

    def _by_type(self, kind, descending=False, limit=None, upper=None, lower=None):
        cond = Key("gsi1pk").eq(kind)
        if upper is not None:
            cond = cond & Key("gsi1sk").lte(upper)
        elif lower is not None:
            cond = cond & Key("gsi1sk").gte(lower)
        items, kwargs = [], {"IndexName": "gsi1", "KeyConditionExpression": cond,
                             "ScanIndexForward": not descending}
        while True:
            page = self.table.query(**kwargs)
            items.extend(page["Items"])
            if "LastEvaluatedKey" not in page or (limit and len(items) >= limit):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return items[:limit] if limit else items

    @staticmethod
    def _level(item):
        return domain.level(item["product_id"], item["warehouse"], item["on_hand"], item["reserved"], item["version"])

    def _product_items(self, pid):
        """Produit et ses niveaux de stock, en lecture cohérente."""
        items = self.table.query(KeyConditionExpression=Key("pk").eq(f"PRODUCT#{pid}"),
                                 ConsistentRead=True)["Items"]
        meta = next((i for i in items if i["sk"] == "META"), None)
        levels = {(pid, i["warehouse"]): self._level(i) for i in items if i["sk"].startswith("STOCK#")}
        return (_clean(meta) if meta else None), levels

    def _catalog(self, ids):
        products, levels = {}, {}
        for pid in ids:
            p, lv = self._product_items(pid)
            if p:
                products[pid] = p
                levels.update(lv)
        return products, levels

    def list_products(self, category=None, include_archived=False):
        products = {i["id"]: _clean(i) for i in self._by_type("PRODUCT")}
        levels = [self._level(i) for i in self._by_type("STOCK")]
        out = [domain.with_stock(domain.with_rating(p), [lv for lv in levels if lv["product_id"] == pid])
               for pid, p in sorted(products.items())]
        return [p for p in out if (include_archived or not p.get("archived"))
                and (not category or p["category"] == category)]

    def get_product(self, product_id, include_archived=False):
        product, levels = self._product_items(product_id)
        if not product or (product.get("archived") and not include_archived):
            raise ApiError(404, "Produit introuvable")
        return domain.with_stock(domain.with_rating(product), list(levels.values()))

    def get_order(self, order_id):
        item = self.table.get_item(Key={"pk": f"ORDER#{order_id}", "sk": "META"}, ConsistentRead=True).get("Item")
        if not item:
            raise ApiError(404, "Commande introuvable")
        return _clean(item)

    def list_orders(self, since):
        """Commandes créées depuis since (tableau de bord, rapport : gsi1 trié par date)."""
        return [_clean(i) for i in self._by_type("ORDER", lower=since)]

    def list_movements(self, product_id=None, limit=100):
        if product_id:
            items = self.table.query(KeyConditionExpression=Key("pk").eq(f"MOVE#{product_id}"),
                                     ScanIndexForward=False, Limit=limit)["Items"]
        else:
            items = self._by_type("MOVE", descending=True, limit=limit)
        return [_clean(i) for i in items]

    def valuation(self):
        products = {i["id"]: _clean(i) for i in self._by_type("PRODUCT")}
        return domain.valuation(products, [self._level(i) for i in self._by_type("STOCK")])

    def low_stock(self):
        return [p for p in self.list_products() if p["stock"] < p["low_stock_threshold"]]

    def export_catalog(self):
        return self.list_products(include_archived=True)

    # --- transactions ----------------------------------------------------------

    def _stock_update(self, pid, wh, on_hand=0, reserved=0):
        """Mise à jour relative d'une ligne de stock, avec les conditions qui empêchent le
        disponible ou le réservé de devenir négatifs."""
        available = on_hand - reserved
        conds, values = [], {":oh": _av(on_hand), ":rs": _av(reserved), ":av": _av(available), ":one": _av(1)}
        if available < 0:
            conds.append("available >= :need")
            values[":need"] = _av(-available)
        if reserved < 0:
            conds.append("reserved >= :rel")
            values[":rel"] = _av(-reserved)
        if on_hand < 0:
            conds.append("on_hand >= :drop")
            values[":drop"] = _av(-on_hand)
        update = {"TableName": self.name,
                  "Key": {"pk": _av(f"PRODUCT#{pid}"), "sk": _av(f"STOCK#{wh}")},
                  "UpdateExpression": "SET on_hand = on_hand + :oh, reserved = reserved + :rs, "
                                      "available = available + :av, version = version + :one",
                  "ExpressionAttributeValues": values}
        update["ConditionExpression"] = " AND ".join(conds) if conds else "attribute_exists(pk)"
        return {"Update": update}

    def _put(self, item, condition=None):
        put = {"TableName": self.name, "Item": {k: _av(v) for k, v in to_item(item).items()}}
        if condition:
            put["ConditionExpression"] = condition
        return {"Put": put}

    def _transact(self, items):
        """Exécute une transaction. Renvoie False si une condition a échoué ou en cas de
        conflit (l'appelant relit et réessaie), lève sinon."""
        if len(items) > 100:
            raise ApiError(400, "Opération trop volumineuse (100 éléments au plus)")
        try:
            self.client.transact_write_items(TransactItems=items)
            return True
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "TransactionCanceledException":
                return False
            if code in ("TransactionConflictException", "ProvisionedThroughputExceededException"):
                return False
            raise

    def _levels_after(self, keys):
        out = []
        for pid in sorted({k[0] for k in keys}):
            _, levels = self._product_items(pid)
            out.extend(lv for k, lv in sorted(levels.items()) if k in keys)
        return out

    @staticmethod
    def _totals(levels, ids):
        return {pid: sum(lv["available"] for (p, _), lv in levels.items() if p == pid) for pid in ids}

    def _retry(self, attempt_fn):
        for attempt in range(RETRIES):
            result = attempt_fn()
            if result is not None:
                return result
            time.sleep(0.05 * (attempt + 1))
        raise ApiError(409, "Trop de modifications simultanées, réessayez")

    # --- commandes ----------------------------------------------------------

    def _cart(self, cart_id):
        return [i for i in self.table.query(KeyConditionExpression=Key("pk").eq(f"CART#{cart_id}"),
                                            ConsistentRead=True)["Items"]]

    def create_order(self, customer, lines, per_product, cart_id=None, promo_code=None, user=None,
                     shipping_method="standard", payment_ttl=domain.PAYMENT_TTL):
        def attempt():
            resv = self._cart(cart_id) if cart_id else []
            held = {}
            for r in resv:
                for a in r["allocations"]:
                    key = (r["product_id"], a["warehouse"])
                    held[key] = held.get(key, 0) + int(a["quantity"])
            ids = set(per_product) | {pid for pid, _ in held}
            products, levels = self._catalog(ids)
            for pid in per_product:
                if pid in products and products[pid].get("archived"):
                    raise ApiError(400, f"Produit indisponible : {pid}")
            items, total = domain.price_lines(lines, products)
            before = self._totals(levels, ids)
            allocations = domain.allocate(per_product, {k: lv["available"] for k, lv in levels.items()}, held)
            delta = {}
            for a in allocations:
                key = (a["product_id"], a["warehouse"])
                oh, rs = delta.get(key, (0, 0))
                delta[key] = (oh - a["quantity"], rs - a["from_reservation"])
                held[key] = held.get(key, 0) - a["from_reservation"]
            for key, left in held.items():
                if left > 0:
                    oh, rs = delta.get(key, (0, 0))
                    delta[key] = (oh, rs - left)
            order = domain.new_order(domain.new_order_id(), customer, items, total, self.backend)
            order["allocations"] = [{k: a[k] for k in ("product_id", "warehouse", "quantity")} for a in allocations]
            promo = self._promo(promo_code) if promo_code else None
            domain.apply_totals(order, domain.promo_discount(promo, total) if promo_code else 0, promo_code,
                                shipping_method)
            order["user_id"] = user["sub"] if user else None
            order["payment_deadline"] = domain.payment_deadline(payment_ttl)
            ops = [self._stock_update(pid, wh, oh, rs) for (pid, wh), (oh, rs) in sorted(delta.items())]
            if promo_code:
                # Compté dans la transaction : jamais plus d'utilisations que max_uses.
                ops.append({"Update": {
                    "TableName": self.name, "Key": {"pk": _av(f"PROMO#{promo_code}"), "sk": _av("META")},
                    "UpdateExpression": "SET uses = uses + :one, version = version + :one",
                    "ConditionExpression": "active = :t AND (attribute_not_exists(max_uses) OR uses < max_uses)",
                    "ExpressionAttributeValues": {":one": _av(1), ":t": _av(True)}}})
            if user:
                ops.append(self._put({**user_order_keys(user["sub"], order), "order_id": order["order_id"],
                                      "product_ids": sorted(per_product)}))
            ops += [{"Delete": {"TableName": self.name, "Key": {"pk": _av(r["pk"]), "sk": _av(r["sk"])},
                                "ConditionExpression": "attribute_exists(pk)"}} for r in resv]
            movements = []
            for a in allocations:
                key = (a["product_id"], a["warehouse"])
                after = levels[key]["on_hand"] - sum(x["quantity"] for x in allocations
                                                     if (x["product_id"], x["warehouse"]) == key)
                movements.append(domain.new_movement(a["product_id"], a["warehouse"], "SALE", -a["quantity"],
                                                     after, ref=order["order_id"]))
            ops += [self._put({**move_keys(m), **m}) for m in movements]
            ops.append(self._put({**order_keys(order), **order}, "attribute_not_exists(pk)"))
            ops.append(self._put(pending_keys(order)))
            if not self._transact(ops):
                return None
            stock = self._levels_after(set(delta))
            after = dict(before)
            after.update(self._totals({(lv["product_id"], lv["warehouse"]): lv for lv in stock},
                                      {lv["product_id"] for lv in stock}))
            return {"order": order, "stock": stock, "movements": movements,
                    "alerts": domain.low_stock_crossings(products, before, after),
                    "promos": [self._promo(promo_code)] if promo_code else []}
        return self._retry(attempt)

    def cancel_order(self, order_id):
        """Serverless : statut CANCELLED et remise en stock dans la même transaction."""
        def attempt():
            order = self.get_order(order_id)
            if order["status"] not in domain.CANCELLABLE:
                raise ApiError(409, f"Commande non annulable (statut {order['status']})")
            now = domain.now_iso()
            ops = [{"Update": {
                "TableName": self.name, "Key": {"pk": _av(f"ORDER#{order_id}"), "sk": _av("META")},
                "UpdateExpression": "SET #s = :c, updated_at = :now, history = list_append(history, :h)",
                "ConditionExpression": "#s = :expected",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":c": _av("CANCELLED"), ":now": _av(now),
                                              ":expected": _av(order["status"]),
                                              ":h": _av([{"status": "CANCELLED", "at": now}])}}},
                   {"Delete": {"TableName": self.name, "Key": {"pk": _av(f"ORDER#{order_id}"), "sk": _av("PENDING")}}}]
            allocations = order.get("allocations", [])
            _, levels = self._catalog({a["product_id"] for a in allocations})
            movements = []
            for a in allocations:
                key = (a["product_id"], a["warehouse"])
                ops.append(self._stock_update(*key, on_hand=int(a["quantity"])))
                m = domain.new_movement(*key, "CANCEL", int(a["quantity"]),
                                        levels[key]["on_hand"] + int(a["quantity"]), ref=order_id)
                movements.append(m)
                ops.append(self._put({**move_keys(m), **m}))
            if not self._transact(ops):
                return None
            ids = {a["product_id"] for a in allocations}
            before = self._totals(levels, ids)
            after = {pid: before[pid] + sum(int(a["quantity"]) for a in allocations if a["product_id"] == pid)
                     for pid in ids}
            return {"order": self.get_order(order_id), "movements": movements,
                    "stock": self._levels_after({(a["product_id"], a["warehouse"]) for a in allocations}),
                    "restocked": domain.restock_crossings(before, after)}
        return self._retry(attempt)

    def mark_cancelled(self, order_id):
        """Hybride (appelé par l'EC2) : passe la commande à CANCELLED si elle n'est pas
        expédiée. Renvoie (commande, déjà annulée avant cet appel) : l'EC2 peut ainsi
        reprendre une annulation interrompue avant la remise en stock."""
        now = domain.now_iso()
        try:
            item = self.table.update_item(
                Key={"pk": f"ORDER#{order_id}", "sk": "META"},
                UpdateExpression="SET #s = :c, updated_at = :now, history = list_append(history, :h)",
                ConditionExpression="#s IN (:p, :a, :b)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":c": "CANCELLED", ":now": now, ":a": "CONFIRMED", ":b": "PACKED",
                                           ":p": "PENDING_PAYMENT", ":h": [{"status": "CANCELLED", "at": now}]},
                ReturnValues="ALL_NEW")["Attributes"]
            return _clean(item), False
        except ClientError as e:
            if not _conditional_failed(e):
                raise
        order = self.get_order(order_id)
        if order["status"] == "CANCELLED":
            return order, True
        raise ApiError(409, f"Commande non annulable (statut {order['status']})")

    # --- paiement (lot 3a) ----------------------------------------------------

    def set_payment(self, order_id, payment):
        """Référence du PaymentIntent (jamais son client_secret). Sans effet si la
        commande est absente (hybride : la projection l'apportera avec son paiement)."""
        try:
            item = self.table.update_item(
                Key={"pk": f"ORDER#{order_id}", "sk": "META"}, UpdateExpression="SET payment = :p",
                ConditionExpression="attribute_exists(pk)", ExpressionAttributeValues={":p": to_item(payment)},
                ReturnValues="ALL_NEW")["Attributes"]
            return _clean(item)
        except ClientError as e:
            if not _conditional_failed(e):
                raise
            return None

    def mark_paid(self, order_id, payment=None):
        """PENDING_PAYMENT -> CONFIRMED, une seule fois (transaction conditionnelle qui
        retire aussi le marqueur d'impayé). Renvoie (commande, déjà payée)."""
        now = domain.now_iso()
        update = "SET #s = :c, updated_at = :now, history = list_append(history, :h)"
        values = {":c": _av("CONFIRMED"), ":now": _av(now), ":p": _av("PENDING_PAYMENT"),
                  ":h": _av([{"status": "CONFIRMED", "at": now}])}
        if payment:
            update += ", payment = :pay"
            values[":pay"] = _av(payment)
        ops = [
            {"Update": {"TableName": self.name, "Key": {"pk": _av(f"ORDER#{order_id}"), "sk": _av("META")},
                        "UpdateExpression": update, "ConditionExpression": "#s = :p",
                        "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": values}},
            {"Delete": {"TableName": self.name, "Key": {"pk": _av(f"ORDER#{order_id}"), "sk": _av("PENDING")}}},
        ]
        for _ in range(RETRIES):
            done = self._transact(ops)
            order = self.get_order(order_id)
            if done:
                return order, False
            if order["status"] in domain.PAID_STATUSES:
                return order, True
            if order["status"] != "PENDING_PAYMENT":
                raise ApiError(409, f"Commande non payable (statut {order['status']})")
            time.sleep(0.05)  # conflit avec une autre transaction : nouvel essai
        raise ApiError(503, "Conflit persistant, réessayez")

    def pending_payments(self, now_iso, limit=50):
        """Commandes impayées dont le délai de paiement est dépassé (purge)."""
        out = []
        for marker in self._by_type("PENDINGPAY", upper=now_iso, limit=limit):
            try:
                order = self.get_order(marker["order_id"])
            except ApiError:
                continue
            if order["status"] == "PENDING_PAYMENT":
                out.append(order)
            else:  # marqueur resté après un essai interrompu
                self.table.delete_item(Key={"pk": marker["pk"], "sk": "PENDING"})
        return out

    # --- retours (lot 3b) ----------------------------------------------------

    def _status_update(self, order_id, expected, new_status, extra="", values=None):
        now = domain.now_iso()
        return {"Update": {
            "TableName": self.name, "Key": {"pk": _av(f"ORDER#{order_id}"), "sk": _av("META")},
            "UpdateExpression": "SET #s = :new, updated_at = :now, history = list_append(history, :h)" + extra,
            "ConditionExpression": "#s = :expected", "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":new": _av(new_status), ":expected": _av(expected), ":now": _av(now),
                                          ":h": _av([{"status": new_status, "at": now}]), **(values or {})}}}

    def request_return(self, order_id, ret):
        """DELIVERED -> RETURN_REQUESTED, avec le marqueur que liste le magasinier."""
        ops = [self._status_update(order_id, "DELIVERED", "RETURN_REQUESTED", ", #r = :ret",
                                   {":ret": _av(ret)}),
               self._put(return_keys(order_id, ret["requested_at"]))]
        ops[0]["Update"]["ExpressionAttributeNames"]["#r"] = "return"
        if not self._transact(ops):
            order = self.get_order(order_id)
            raise ApiError(409, f"Retour impossible pour une commande {order['status']}")
        return {"order": self.get_order(order_id)}

    def mark_returned(self, order_id):
        """RETURN_REQUESTED -> RETURNED (et retire le marqueur). Renvoie (commande, déjà
        réceptionnée) : comme mark_cancelled, une réception interrompue peut être reprise."""
        now = domain.now_iso()
        ops = [self._status_update(order_id, "RETURN_REQUESTED", "RETURNED", ", #r.received_at = :now"),
               {"Delete": {"TableName": self.name, "Key": {"pk": _av(f"ORDER#{order_id}"), "sk": _av("RETURN")}}}]
        ops[0]["Update"]["ExpressionAttributeNames"]["#r"] = "return"
        ops[0]["Update"]["ExpressionAttributeValues"][":now"] = _av(now)
        for _ in range(RETRIES):
            done = self._transact(ops)
            order = self.get_order(order_id)
            if done:
                return order, False
            if order["status"] == "RETURNED":
                return order, True
            if order["status"] != "RETURN_REQUESTED":
                raise ApiError(409, f"Aucun retour à réceptionner (statut {order['status']})")
            time.sleep(0.05)
        raise ApiError(503, "Conflit persistant, réessayez")

    def receive_return(self, order_id):
        """Serverless : RETURNED et remise en stock (mouvements RETURN) dans la même
        transaction. Renvoie aussi already=True si le retour était déjà réceptionné."""
        def attempt():
            order = self.get_order(order_id)
            if order["status"] == "RETURNED":
                return {"order": order, "stock": [], "movements": [], "restocked": [], "already": True}
            if order["status"] != "RETURN_REQUESTED":
                raise ApiError(409, f"Aucun retour à réceptionner (statut {order['status']})")
            now = domain.now_iso()
            ops = [self._status_update(order_id, "RETURN_REQUESTED", "RETURNED", ", #r.received_at = :rcv",
                                       {":rcv": _av(now)}),
                   {"Delete": {"TableName": self.name, "Key": {"pk": _av(f"ORDER#{order_id}"), "sk": _av("RETURN")}}}]
            ops[0]["Update"]["ExpressionAttributeNames"]["#r"] = "return"
            allocations = order.get("allocations", [])
            _, levels = self._catalog({a["product_id"] for a in allocations})
            movements = []
            for a in allocations:
                key = (a["product_id"], a["warehouse"])
                ops.append(self._stock_update(*key, on_hand=int(a["quantity"])))
                m = domain.new_movement(*key, "RETURN", int(a["quantity"]),
                                        levels[key]["on_hand"] + int(a["quantity"]), ref=order_id)
                movements.append(m)
                ops.append(self._put({**move_keys(m), **m}))
            if not self._transact(ops):
                return None
            ids = {a["product_id"] for a in allocations}
            before = self._totals(levels, ids)
            after = {pid: before[pid] + sum(int(a["quantity"]) for a in allocations if a["product_id"] == pid)
                     for pid in ids}
            return {"order": self.get_order(order_id), "movements": movements, "already": False,
                    "stock": self._levels_after({(a["product_id"], a["warehouse"]) for a in allocations}),
                    "restocked": domain.restock_crossings(before, after)}
        return self._retry(attempt)

    def list_returns(self, limit=100):
        """Retours à réceptionner, du plus ancien au plus récent."""
        out = []
        for marker in self._by_type("RETURNREQ", limit=limit):
            try:
                order = self.get_order(marker["order_id"])
            except ApiError:
                continue
            if order["status"] == "RETURN_REQUESTED":
                out.append(order)
        return out

    # --- réapprovisionnement fournisseur (lot 3b) -----------------------------

    def get_purchase_order(self, po_id):
        item = self.table.get_item(Key={"pk": f"PO#{po_id}", "sk": "META"}, ConsistentRead=True).get("Item")
        if not item:
            raise ApiError(404, "Bon de commande introuvable")
        return _clean(item)

    def open_purchase_order(self, product_id, alert=None):
        """Crée le bon de commande d'un produit, sauf s'il en a déjà un ouvert (marqueur
        SUPPLY#<produit> écrit sous condition dans la même transaction). Renvoie (bon, créé)."""
        product, _ = self._product_items(product_id)
        if not product:
            raise ApiError(404, "Produit introuvable")
        po = domain.new_purchase_order(product, alert)
        marker = {"pk": f"SUPPLY#{product_id}", "sk": "OPEN", "entity": "SUPPLY", "po_id": po["po_id"]}
        if self._transact([self._put(marker, "attribute_not_exists(pk)"), self._put({**po_keys(po), **po})]):
            return po, True
        current = self.table.get_item(Key={"pk": marker["pk"], "sk": "OPEN"},
                                      ConsistentRead=True).get("Item")
        if not current:
            raise ApiError(409, "Bon de commande en cours de création, réessayez")
        return self.get_purchase_order(current["po_id"]), False

    def receive_purchase_order(self, po_id):
        """Livraison du fournisseur : bon RECEIVED, stock + quantité (mouvement RESTOCK
        de référence le bon) et marqueur retiré, en une transaction. Rejouée : already."""
        def attempt():
            po = self.get_purchase_order(po_id)
            if po["status"] == "RECEIVED":
                return {"purchase_orders": [po], "stock": [], "movements": [], "restocked": [], "already": True}
            products, levels = self._catalog([po["product_id"]])
            key = (po["product_id"], po["warehouse"])
            qty, now = int(po["quantity"]), domain.now_iso()
            m = domain.new_movement(*key, "RESTOCK", qty, levels[key]["on_hand"] + qty, ref=po_id,
                                    reason=f"Livraison fournisseur {po['supplier']}")
            received = {**po, "status": "RECEIVED", "received_at": now, "updated_at": now,
                        "version": int(po["version"]) + 1}
            ops = [self._put({**po_keys(received), **received}, "#s = :open"),
                   {"Delete": {"TableName": self.name, "Key": {"pk": _av(f"SUPPLY#{po['product_id']}"), "sk": _av("OPEN")}}},
                   self._stock_update(*key, on_hand=qty), self._put({**move_keys(m), **m})]
            ops[0]["Put"]["ExpressionAttributeNames"] = {"#s": "status"}
            ops[0]["Put"]["ExpressionAttributeValues"] = {":open": _av("OPEN")}
            if not self._transact(ops):
                return None
            before = self._totals(levels, [po["product_id"]])
            after = {po["product_id"]: before[po["product_id"]] + qty}
            return {"purchase_orders": [received], "movements": [m], "already": False,
                    "stock": self._levels_after({key}), "restocked": domain.restock_crossings(before, after)}
        return self._retry(attempt)

    def list_purchase_orders(self, limit=100):
        return [_clean(i) for i in self._by_type("PO", descending=True, limit=limit)]

    # --- réservations -------------------------------------------------------

    def reserve(self, cart_id, product_id, quantity, ttl):
        def attempt():
            products, levels = self._catalog([product_id])
            if not products or products[product_id].get("archived"):
                raise ApiError(404, "Produit introuvable")
            existing = self.table.get_item(Key={"pk": f"CART#{cart_id}", "sk": f"RESV#{product_id}"},
                                           ConsistentRead=True).get("Item")
            current = {a["warehouse"]: int(a["quantity"]) for a in existing["allocations"]} if existing else {}
            before = self._totals(levels, [product_id])
            change = {}
            delta = quantity - sum(current.values())
            if delta > 0:
                free = {k: lv["available"] for k, lv in levels.items()}
                for a in domain.allocate({product_id: delta}, free):
                    current[a["warehouse"]] = current.get(a["warehouse"], 0) + a["quantity"]
                    change[a["warehouse"]] = a["quantity"]
            elif delta < 0:
                give_back = -delta
                for wh in reversed(WAREHOUSES):
                    use = min(current.get(wh, 0), give_back)
                    if use:
                        current[wh] -= use
                        give_back -= use
                        change[wh] = -use
            allocations = [{"warehouse": wh, "quantity": q} for wh, q in current.items() if q > 0]
            ops = [self._stock_update(product_id, wh, reserved=q) for wh, q in sorted(change.items())]
            key = {"pk": _av(f"CART#{cart_id}"), "sk": _av(f"RESV#{product_id}")}
            guard = ("attribute_not_exists(pk)" if not existing else "quantity = :q0 AND expires_at = :e0")
            guard_values = {} if not existing else {":q0": _av(existing["quantity"]), ":e0": _av(existing["expires_at"])}
            expires = domain.expires_at(ttl) if quantity else None
            if quantity == 0:
                if existing:
                    ops.append({"Delete": {"TableName": self.name, "Key": key, "ConditionExpression": guard,
                                           "ExpressionAttributeValues": guard_values}})
            else:
                item = {**resv_keys(cart_id, product_id, expires), "cart_id": cart_id, "product_id": product_id,
                        "quantity": quantity, "allocations": allocations, "expires_at": expires,
                        "ttl": int(time.time()) + ttl + 86400}
                put = self._put(item, guard)
                if guard_values:
                    put["Put"]["ExpressionAttributeValues"] = guard_values
                ops.append(put)
            if not ops:
                return {"reservation": {"cart_id": cart_id, "product_id": product_id, "quantity": 0,
                                        "allocations": [], "expires_at": None}, "stock": [], "alerts": []}
            if not self._transact(ops):
                return None
            stock = self._levels_after({(product_id, wh) for wh in change})
            after = {product_id: before[product_id] - sum(change.values())}
            return {"reservation": {"cart_id": cart_id, "product_id": product_id, "quantity": quantity,
                                    "allocations": allocations, "expires_at": expires},
                    "stock": stock, "alerts": domain.low_stock_crossings(products, before, after),
                    "restocked": domain.restock_crossings(before, after)}
        return self._retry(attempt)

    def _release_items(self, items):
        stock_keys_changed, released = set(), {}
        for r in items:
            ops = [{"Delete": {"TableName": self.name, "Key": {"pk": _av(r["pk"]), "sk": _av(r["sk"])},
                               "ConditionExpression": "expires_at = :e",
                               "ExpressionAttributeValues": {":e": _av(r["expires_at"])}}}]
            ops += [self._stock_update(r["product_id"], a["warehouse"], reserved=-int(a["quantity"]))
                    for a in r["allocations"]]
            if self._transact(ops):  # sinon : déjà libérée ou prolongée entre-temps
                stock_keys_changed |= {(r["product_id"], a["warehouse"]) for a in r["allocations"]}
                for a in r["allocations"]:
                    released[r["product_id"]] = released.get(r["product_id"], 0) + int(a["quantity"])
        if not stock_keys_changed:
            return {"stock": [], "restocked": []}
        _, levels = self._catalog(released)
        after = self._totals(levels, released)
        before = {pid: after[pid] - qty for pid, qty in released.items()}
        return {"stock": self._levels_after(stock_keys_changed), "restocked": domain.restock_crossings(before, after)}

    def release(self, cart_id, product_id=None):
        items = [i for i in self._cart(cart_id) if not product_id or i["product_id"] == product_id]
        return self._release_items(items)

    def expire_reservations(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self._release_items(self._by_type("RESV", upper=now))

    # --- administration -----------------------------------------------------

    def apply_movement(self, product_id, warehouse, kind, quantity, reason=""):
        def attempt():
            products, levels = self._catalog([product_id])
            if not products:
                raise ApiError(404, "Produit introuvable")
            lv = levels[(product_id, warehouse)]
            before = self._totals(levels, [product_id])
            delta = domain.movement_delta(kind, quantity, lv["on_hand"])
            if lv["on_hand"] + delta < lv["reserved"]:
                raise ApiError(409, "Stock insuffisant : des unités sont réservées ou le stock deviendrait négatif")
            m = domain.new_movement(product_id, warehouse, kind, delta, lv["on_hand"] + delta, reason=reason)
            op = self._stock_update(product_id, warehouse, on_hand=delta)
            # Inventaire : valeur absolue, valable seulement si personne n'a touché la ligne.
            op["Update"]["ConditionExpression"] = "version = :ver"
            op["Update"]["ExpressionAttributeValues"][":ver"] = _av(lv["version"])
            for k in (":need", ":rel", ":drop"):
                op["Update"]["ExpressionAttributeValues"].pop(k, None)
            if not self._transact([op, self._put({**move_keys(m), **m})]):
                return None
            stock = self._levels_after({(product_id, warehouse)})
            after = self._totals({(x["product_id"], x["warehouse"]): x for x in stock}, [product_id])
            other = {k: v for k, v in levels.items() if k != (product_id, warehouse)}
            after[product_id] += sum(v["available"] for v in other.values())
            return {"stock": stock, "movements": [m],
                    "alerts": domain.low_stock_crossings(products, before, after),
                    "restocked": domain.restock_crossings(before, after)}
        return self._retry(attempt)

    def create_product(self, product_id, fields):
        ops = [self._put({**product_keys(product_id), "id": product_id, **fields, "version": 1},
                         "attribute_not_exists(pk)")]
        ops += [self._put({**stock_keys(product_id, wh), "product_id": product_id, "warehouse": wh, "on_hand": 0,
                           "reserved": 0, "available": 0, "version": 1}) for wh in WAREHOUSES]
        if not self._transact(ops):
            raise ApiError(409, f"Le produit {product_id} existe déjà")
        product = self.get_product(product_id, include_archived=True)
        return {"products": [product], "stock": self._levels_after({(product_id, wh) for wh in WAREHOUSES})}

    def update_product(self, product_id, fields):
        if not fields:
            raise ApiError(400, "Aucun champ à modifier")
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": to_item(v) if isinstance(v, (list, dict)) else v for i, v in enumerate(fields.values())}
        values[":one"] = 1
        try:
            self.table.update_item(
                Key={"pk": f"PRODUCT#{product_id}", "sk": "META"},
                UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
                                 + ", version = version + :one",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        except ClientError as e:
            if _conditional_failed(e):
                raise ApiError(404, "Produit introuvable")
            raise
        return {"products": [self.get_product(product_id, include_archived=True)]}

    def import_catalog(self, rows):
        """Crée ou met à jour les produits du CSV, puis fixe le stock par inventaire.
        Atomique par produit (pas sur tout le fichier : 100 éléments au plus par transaction)."""
        created = updated = 0
        products, stock, movements, alerts, restocked = [], [], [], [], []
        for pid, fields, wh_stock in rows:
            existing, _ = self._product_items(pid)
            if existing:
                if fields:
                    self.update_product(pid, fields)
                    updated += 1
            else:
                _, full = domain.validate_product({**fields, "id": pid})
                self.create_product(pid, full)
                created += 1
            for wh, target in wh_stock.items():
                _, levels = self._product_items(pid)
                if levels[(pid, wh)]["on_hand"] != target:
                    res = self.apply_movement(pid, wh, "INVENTORY", target, reason="import CSV")
                    stock += res["stock"]
                    movements += res["movements"]
                    alerts += res["alerts"]
                    restocked += res["restocked"]
            products.append(self.get_product(pid, include_archived=True))
        return {"created": created, "updated": updated, "products": products, "stock": stock,
                "movements": movements, "alerts": alerts, "restocked": sorted(set(restocked))}

    # --- lot 2a : commandes d'un client, codes promo, avis, souhaits ----------

    def _promo(self, code):
        item = self.table.get_item(Key={"pk": f"PROMO#{code}", "sk": "META"}, ConsistentRead=True).get("Item")
        return _clean(item) if item else None

    def my_orders(self, user_id, limit=50):
        index = self.table.query(KeyConditionExpression=Key("pk").eq(f"USER#{user_id}") & Key("sk").begins_with("ORDER#"),
                                 ScanIndexForward=False, Limit=limit)["Items"]
        out = []
        for i in index:
            try:
                out.append(self.get_order(i["order_id"]))
            except ApiError:
                pass  # hybride : commande pas encore projetée
        return out

    def list_promos(self):
        return [_clean(i) for i in self._by_type("PROMO")]

    def save_promo(self, code, fields, create):
        if create:
            item = {**promo_keys(code), "code": code, **{k: v for k, v in fields.items() if v is not None},
                    "uses": 0, "version": 1}
            try:
                self.table.put_item(Item=to_item(item), ConditionExpression="attribute_not_exists(pk)")
            except ClientError as e:
                if _conditional_failed(e):
                    raise ApiError(409, f"Le code {code} existe déjà")
                raise
        else:
            if not fields:
                raise ApiError(400, "Aucun champ à modifier")
            sets = {k: v for k, v in fields.items() if v is not None}
            removes = [k for k, v in fields.items() if v is None]  # max_uses ou expires_at effacés
            names = {f"#f{i}": k for i, k in enumerate(list(sets) + removes)}
            values = {f":v{i}": (to_item(v) if isinstance(v, (float, dict, list)) else v) for i, v in enumerate(sets.values())}
            values[":one"] = 1
            expr = "SET " + ", ".join([f"#f{i} = :v{i}" for i in range(len(sets))] + ["version = version + :one"])
            if removes:
                expr += " REMOVE " + ", ".join(f"#f{len(sets) + i}" for i in range(len(removes)))
            try:
                self.table.update_item(Key={"pk": f"PROMO#{code}", "sk": "META"}, UpdateExpression=expr,
                                       ConditionExpression="attribute_exists(pk)", ExpressionAttributeNames=names,
                                       ExpressionAttributeValues=values)
            except ClientError as e:
                if _conditional_failed(e):
                    raise ApiError(404, "Code promo introuvable")
                raise
        return {"promos": [self._promo(code)]}

    def list_reviews(self, product_id):
        items = self.table.query(
            KeyConditionExpression=Key("pk").eq(f"PRODUCT#{product_id}") & Key("sk").begins_with("REVIEW#"))["Items"]
        return sorted((_clean(i) for i in items), key=lambda r: r["at"], reverse=True)

    def _has_bought(self, user_id, product_id):
        index = self.table.query(KeyConditionExpression=Key("pk").eq(f"USER#{user_id}") & Key("sk").begins_with("ORDER#"),
                                 ConsistentRead=True)["Items"]
        for i in index:
            if product_id in i.get("product_ids", []):
                try:
                    if self.get_order(i["order_id"])["status"] != "CANCELLED":
                        return True
                except ApiError:
                    pass
        return False

    def add_review(self, product_id, user, rating, comment):
        """Un avis par client et par produit (le dernier remplace le précédent), réservé
        aux clients qui ont commandé le produit. Avis et note moyenne dans une transaction."""
        def attempt():
            product, _ = self._product_items(product_id)
            if not product or product.get("archived"):
                raise ApiError(404, "Produit introuvable")
            if not self._has_bought(user["sub"], product_id):
                raise ApiError(403, "Seuls les clients ayant commandé ce produit peuvent laisser un avis")
            old = self.table.get_item(Key={"pk": f"PRODUCT#{product_id}", "sk": f"REVIEW#{user['sub']}"},
                                      ConsistentRead=True).get("Item")
            review = domain.new_review(product_id, user, rating, comment)
            put = self._put({**review_keys(product_id, user["sub"]), **review},
                            "rating = :old" if old else "attribute_not_exists(pk)")
            if old:
                put["Put"]["ExpressionAttributeValues"] = {":old": _av(old["rating"])}
            ops = [put, {"Update": {
                "TableName": self.name, "Key": {"pk": _av(f"PRODUCT#{product_id}"), "sk": _av("META")},
                "UpdateExpression": "SET rating_sum = if_not_exists(rating_sum, :zero) + :d, "
                                    "rating_count = if_not_exists(rating_count, :zero) + :c, version = version + :one",
                "ExpressionAttributeValues": {":zero": _av(0), ":one": _av(1),
                                              ":d": _av(rating - (int(old["rating"]) if old else 0)),
                                              ":c": _av(0 if old else 1)}}}]
            if not self._transact(ops):
                return None
            return {"review": review, "reviews": [review],
                    "products": [self.get_product(product_id, include_archived=True)]}
        return self._retry(attempt)

    def wishlist(self, user_id):
        items = self.table.query(KeyConditionExpression=Key("pk").eq(f"USER#{user_id}") & Key("sk").begins_with("WISH#"),
                                 ConsistentRead=True)["Items"]
        out = []
        for i in items:
            try:
                out.append(self.get_product(i["product_id"]))
            except ApiError:
                pass  # produit archivé
        return out

    def set_wish(self, user_id, product_id, wished, email=None):
        if wished:
            self.get_product(product_id)  # 404 si inconnu ou archivé
            self.table.put_item(Item={**wish_keys(user_id, product_id), "user_id": user_id, "product_id": product_id,
                                      "created_at": domain.now_iso(), **({"email": email} if email else {})})
        else:
            self.table.delete_item(Key={"pk": f"USER#{user_id}", "sk": f"WISH#{product_id}"})
        return {"wishes": [{"user_id": user_id, "product_id": product_id, "wished": wished, "email": email}]}

    def notifications(self, user_id, limit=50):
        items = self.table.query(KeyConditionExpression=Key("pk").eq(f"USER#{user_id}") & Key("sk").begins_with("NOTIF#"),
                                 ScanIndexForward=False, Limit=limit)["Items"]
        return [_clean(i) for i in items]

    def notify_back_in_stock(self, product_ids):
        """Notifie les clients qui ont le produit dans leur liste de souhaits."""
        out = []
        for pid in product_ids or []:
            try:
                product = self.get_product(pid)
            except ApiError:
                continue
            for wish in self._by_type(f"WISH#{pid}"):
                n = domain.new_notification(wish["user_id"], product, wish.get("email"))
                self.table.put_item(Item={**notif_keys(n), **n})
                out.append(n)
        return out

    def _project_extras(self, event):
        """Projection (hybride) des codes promo, avis, souhaits, notifications et bons de commande."""
        for promo in event.get("promos") or []:
            try:
                self.table.put_item(Item=to_item({**promo_keys(promo["code"]),
                                                  **{k: v for k, v in promo.items() if v is not None}}),
                                    ConditionExpression="attribute_not_exists(pk) OR version < :v",
                                    ExpressionAttributeValues={":v": int(promo["version"])})
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
        for review in event.get("reviews") or []:
            self.table.put_item(Item=to_item({**review_keys(review["product_id"], review["user_id"]), **review}))
        for wish in event.get("wishes") or []:
            if wish["wished"]:
                self.table.put_item(Item={**wish_keys(wish["user_id"], wish["product_id"]), "user_id": wish["user_id"],
                                          "product_id": wish["product_id"], "created_at": domain.now_iso(),
                                          **({"email": wish["email"]} if wish.get("email") else {})})
            else:
                self.table.delete_item(Key={"pk": f"USER#{wish['user_id']}", "sk": f"WISH#{wish['product_id']}"})
        for po in event.get("purchase_orders") or []:
            try:
                self.table.put_item(Item=to_item({**po_keys(po), **po}),
                                    ConditionExpression="attribute_not_exists(pk) OR version < :v",
                                    ExpressionAttributeValues={":v": int(po["version"])})
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
        for n in event.get("notifications") or []:
            try:
                self.table.put_item(Item=to_item({**notif_keys(n), **n}), ConditionExpression="attribute_not_exists(pk)")
            except ClientError as e:
                if not _conditional_failed(e):
                    raise

    # --- traitement interne (Lambda processor) -------------------------------

    def create_order_if_absent(self, order):
        """Hybride : la commande vient de RDS par ORDER_CREATED ; le premier consommateur
        (projector ou processor) l'écrit, sans attendre l'autre."""
        if order.get("user_id"):  # index « commandes du client » (idempotent)
            self.table.put_item(Item={**user_order_keys(order["user_id"], order), "order_id": order["order_id"],
                                      "product_ids": sorted({i["product_id"] for i in order["items"]})})
        try:
            self.table.put_item(Item=to_item({**order_keys(order), **order}),
                                ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as e:
            if not _conditional_failed(e):
                raise
            return False

    def advance(self, event):
        """Applique la transition de l'événement. Renvoie (commande, événement suivant, rejoué)
        ou None si l'événement est un vrai doublon (ou la commande annulée). Mise à jour
        conditionnelle : une transition n'est jamais appliquée deux fois ; si le statut est
        déjà le statut cible (essai précédent interrompu avant la publication), l'événement
        suivant est republié."""
        transition = domain.TRANSITIONS.get(event["event_type"])
        if not transition:
            return None
        expected, new_status, next_event = transition
        order_id = event["order"]["order_id"]
        now = domain.now_iso()
        update = "SET #s = :new, updated_at = :now, history = list_append(history, :h)"
        values = {":new": new_status, ":expected": expected, ":now": now,
                  ":h": [{"status": new_status, "at": now}]}
        if new_status == "SHIPPED":
            current = self.table.get_item(Key={"pk": f"ORDER#{order_id}", "sk": "META"}).get("Item") or {}
            update += ", shipping = :ship"
            values[":ship"] = domain.new_shipping(current.get("shipping_method", "standard"))
        try:
            order = self.table.update_item(
                Key={"pk": f"ORDER#{order_id}", "sk": "META"}, UpdateExpression=update,
                ConditionExpression="#s = :expected", ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values, ReturnValues="ALL_NEW")["Attributes"]
            return _clean(order), next_event, False
        except ClientError as e:
            if not _conditional_failed(e):
                raise
        order = self.table.get_item(Key={"pk": f"ORDER#{order_id}", "sk": "META"}, ConsistentRead=True).get("Item")
        if not order:
            raise RuntimeError(f"commande {order_id} absente de la table, nouvel essai plus tard")
        if order["status"] != new_status:
            return None
        return _clean(order), next_event, True

    # --- projection CQRS (Lambda projector, hybride) --------------------------

    def project(self, event):
        """Recopie dans le modèle de lecture un événement venant de RDS. Chaque ligne de
        stock et chaque produit ne sont appliqués que si leur version est plus récente ;
        commandes et mouvements sont écrits une seule fois. Renvoie les niveaux ignorés."""
        kind = event["event_type"]
        if kind == "ORDER_CREATED":
            self.create_order_if_absent(event["order"])
        self._project_extras(event)
        for product in event.get("products", []) or []:
            self._project_product(product)
        for m in event.get("movements", []) or []:
            try:
                self.table.put_item(Item=to_item({**move_keys(m), **m}), ConditionExpression="attribute_not_exists(pk)")
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
        ignored = []
        for lv in event.get("stock", []) or []:
            try:
                self.table.put_item(
                    Item=to_item({**stock_keys(lv["product_id"], lv["warehouse"]), **lv}),
                    ConditionExpression="attribute_not_exists(pk) OR version < :v",
                    ExpressionAttributeValues={":v": lv["version"]})
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
                ignored.append(lv)
        return ignored

    def _project_product(self, product):
        item = {k: v for k, v in product.items() if k not in ("stock", "stock_by_warehouse")}
        try:
            self.table.put_item(Item=to_item({**product_keys(product["id"]), **item}),
                                ConditionExpression="attribute_not_exists(pk) OR version < :v",
                                ExpressionAttributeValues={":v": int(product["version"])})
        except ClientError as e:
            if not _conditional_failed(e):
                raise
