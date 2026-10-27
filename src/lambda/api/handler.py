"""API de la boutique, exposée via une Lambda Function URL derrière CloudFront.

- Solution serverless : toutes les routes.
- Solution hybride (READ_ONLY=1, BACKEND=hybrid) : lectures seulement (catalogue,
  suivi). Les commandes sont créées par l'EC2 hybride, les tables sont des
  projections mises à jour par les événements.

Routes :
  GET  /api/health
  GET  /api/products[?category=tshirt|pull|baskets]
  GET  /api/products/{id}
  POST /api/orders          {"customer": {"name", "email"}, "items": [{"product_id", "size", "quantity"}]}
  GET  /api/orders/{id}
"""

import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
ddb_client = boto3.client("dynamodb")
sns = boto3.client("sns")

PRODUCTS_TABLE = os.environ["PRODUCTS_TABLE"]
ORDERS_TABLE = os.environ["ORDERS_TABLE"]
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
ORIGIN_SECRET = os.environ["ORIGIN_SECRET"]
BACKEND = os.environ.get("BACKEND", "serverless")
READ_ONLY = os.environ.get("READ_ONLY") == "1"

products_table = dynamodb.Table(PRODUCTS_TABLE)
orders_table = dynamodb.Table(ORDERS_TABLE)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_LINES = 20
MAX_QTY = 10


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_json(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(f"Type non sérialisable : {type(obj)}")


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=to_json, ensure_ascii=False),
    }


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def list_products(params):
    items, kwargs = [], {}
    while True:
        page = products_table.scan(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    category = (params or {}).get("category")
    if category:
        items = [p for p in items if p["category"] == category]
    return sorted(items, key=lambda p: p["id"])


def get_product(product_id):
    item = products_table.get_item(Key={"id": product_id}).get("Item")
    if not item:
        raise ApiError(404, "Produit introuvable")
    return item


def parse_order(body):
    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError:
        raise ApiError(400, "JSON invalide")

    customer = data.get("customer") or {}
    name = str(customer.get("name", "")).strip()
    email = str(customer.get("email", "")).strip()
    if not name or len(name) > 100:
        raise ApiError(400, "Nom du client invalide")
    if not EMAIL_RE.match(email):
        raise ApiError(400, "Adresse e-mail invalide")

    lines = data.get("items") or []
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        raise ApiError(400, f"La commande doit contenir entre 1 et {MAX_LINES} articles")

    # Regroupe les lignes identiques et calcule la quantité totale par produit
    # (une transaction DynamoDB ne peut toucher qu'une fois chaque élément).
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
    return {"name": name, "email": email}, merged, per_product


def create_order(body):
    customer, lines, per_product = parse_order(body)

    keys = [{"id": {"S": pid}} for pid in per_product]
    found = ddb_client.batch_get_item(RequestItems={PRODUCTS_TABLE: {"Keys": keys}})["Responses"][PRODUCTS_TABLE]
    catalog = {p["id"]["S"]: p for p in found}

    items, total = [], Decimal("0")
    for (pid, size), qty in lines.items():
        product = catalog.get(pid)
        if not product:
            raise ApiError(400, f"Produit inconnu : {pid}")
        if size not in [s["S"] for s in product["sizes"]["L"]]:
            raise ApiError(400, f"Taille {size} indisponible pour {product['name']['S']}")
        price = Decimal(product["price"]["N"])
        total += price * qty
        items.append({"product_id": pid, "name": product["name"]["S"], "size": size,
                      "quantity": qty, "unit_price": price})

    order_id = "ORD-" + uuid.uuid4().hex[:10].upper()
    created_at = now_iso()
    order = {
        "order_id": order_id,
        "status": "CONFIRMED",
        "customer": customer,
        "items": items,
        "total": total,
        "created_at": created_at,
        "updated_at": created_at,
        "history": [{"status": "CONFIRMED", "at": created_at}],
        "backend": "serverless",
    }

    # Transaction atomique : décrément du stock (avec contrôle) + création de la commande.
    transact = [
        {
            "Update": {
                "TableName": PRODUCTS_TABLE,
                "Key": {"id": {"S": pid}},
                "UpdateExpression": "SET stock = stock - :q",
                "ConditionExpression": "stock >= :q",
                "ExpressionAttributeValues": {":q": {"N": str(qty)}},
            }
        }
        for pid, qty in per_product.items()
    ]
    serializer = TypeSerializer()
    transact.append({
        "Put": {
            "TableName": ORDERS_TABLE,
            "Item": {k: serializer.serialize(v) for k, v in order.items()},
            "ConditionExpression": "attribute_not_exists(order_id)",
        }
    })
    try:
        ddb_client.transact_write_items(TransactItems=transact)
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            out = [list(per_product)[i] for i, r in enumerate(reasons[:len(per_product)])
                   if r.get("Code") == "ConditionalCheckFailed"]
            raise ApiError(409, "Stock insuffisant pour : " + ", ".join(out) if out else "Commande refusée, réessayez")
        raise

    publish_event("ORDER_CREATED", order)
    return order


def publish_event(event_type, order):
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": now_iso(),
        "source": "serverless",
        "order": order,
    }
    lines = "\n".join(f"  - {i['quantity']} x {i['name']} (taille {i['size']})" for i in order["items"])
    email = (
        f"Bonjour {order['customer']['name']},\n\n"
        f"Votre commande {order['order_id']} est confirmée.\n\n{lines}\n\n"
        f"Total : {order['total']} EUR\n\nMerci pour votre achat !"
    )
    sns.publish(
        TopicArn=TOPIC_ARN,
        MessageStructure="json",
        Message=json.dumps({
            "default": json.dumps(event, default=to_json),
            "email": email,
        }),
        Subject=f"Commande {order['order_id']} : confirmation",
        MessageAttributes={
            "event_type": {"DataType": "String", "StringValue": event_type},
            "source": {"DataType": "String", "StringValue": "serverless"},
        },
    )


def get_order(order_id):
    item = orders_table.get_item(Key={"order_id": order_id}, ConsistentRead=True).get("Item")
    if not item:
        raise ApiError(404, "Commande introuvable")
    return item


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("x-origin-verify") != ORIGIN_SECRET:
        return response(403, {"error": "Accès direct interdit, passez par CloudFront"})

    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/").rstrip("/") or "/"
    params = event.get("queryStringParameters") or {}

    try:
        if method == "GET" and path == "/api/health":
            return response(200, {"status": "ok", "backend": BACKEND, "region": os.environ["AWS_REGION"]})
        if method == "GET" and path == "/api/products":
            return response(200, {"products": list_products(params)})
        if method == "GET" and path.startswith("/api/products/"):
            return response(200, get_product(path.split("/")[-1]))
        if method == "POST" and path == "/api/orders" and not READ_ONLY:
            body = event.get("body") or ""
            if event.get("isBase64Encoded"):
                body = base64.b64decode(body).decode("utf-8")
            return response(201, create_order(body))
        if method == "GET" and path.startswith("/api/orders/"):
            return response(200, get_order(path.split("/")[-1]))
        if method == "GET" and path == "/api/stats":
            return response(200, {"enabled": False})
        return response(404, {"error": "Route inconnue"})
    except ApiError as e:
        return response(e.status, {"error": e.message})
