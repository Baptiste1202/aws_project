"""Boutique en ligne - solutions VM et hybride (EC2 + RDS PostgreSQL).

Lancé par systemd sur chaque instance de l'Auto Scaling Group, une fois par solution :
  - SHOP_SOURCE=vm (port 8080) : serveur HTTP (site statique + API identique à la
    version serverless), worker SQS "vm-processing" (emballage puis expédition
    simulés) et, si MSK est activé, pont SQS -> Kafka et stats temps réel ;
  - SHOP_SOURCE=hybrid (port 8081, base shop_hybrid) : seul POST /api/orders lui est
    envoyé par CloudFront. Pas de worker : le traitement est fait par des Lambdas.
Chaque ORDER_CREATED porte le stock après la commande (avec sa version), pour la
projection du catalogue dans DynamoDB (solution hybride).
"""

import json
import mimetypes
import os
import random
import re
import socket
import threading
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool

REGION = os.environ["AWS_REGION"]
PORT = int(os.environ.get("PORT", "8080"))
STATIC_DIR = Path(os.environ["STATIC_DIR"]).resolve()
PRODUCTS_FILE = os.environ["PRODUCTS_FILE"]
TOPIC_ARN = os.environ["TOPIC_ARN"]
QUEUE_URL = os.environ.get("QUEUE_URL", "")
SOURCE = os.environ.get("SHOP_SOURCE", "vm")
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "")
KAFKA_QUEUE_URL = os.environ.get("KAFKA_QUEUE_URL", "")
KAFKA_TOPIC = "order-events"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_LINES = 20
MAX_QTY = 10
CARRIERS = ["Colissimo", "Chronopost", "DHL Express", "Mondial Relay"]
TRANSITIONS = {
    "ORDER_CREATED": ("CONFIRMED", "PACKED", "ORDER_PACKED"),
    "ORDER_PACKED": ("PACKED", "SHIPPED", "ORDER_SHIPPED"),
}

sns = boto3.client("sns", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)


def log(**fields):
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_json(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    raise TypeError(f"Type non sérialisable : {type(obj)}")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    price       NUMERIC(10, 2) NOT NULL,
    color       TEXT NOT NULL,
    hex         TEXT NOT NULL,
    sizes       JSONB NOT NULL,
    stock       INTEGER NOT NULL CHECK (stock >= 0),
    version     BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    order_id   TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    customer   JSONB NOT NULL,
    items      JSONB NOT NULL,
    total      NUMERIC(10, 2) NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    history    JSONB NOT NULL,
    shipping   JSONB
);
ALTER TABLE products ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;
"""

POOL_SIZE = 10
POOL_WAIT_SECONDS = 10

pool = None
# psycopg2 lève une erreur quand le pool est vide : ce sémaphore fait attendre
# la requête qu'une connexion se libère (au plus POOL_WAIT_SECONDS).
pool_slots = threading.BoundedSemaphore(POOL_SIZE)


def create_database(password):
    """Crée la base DB_NAME si elle n'existe pas (la solution hybride a sa propre base
    sur la même instance RDS que la solution VM)."""
    conn = psycopg2.connect(host=os.environ["DB_HOST"], dbname="postgres", user=os.environ["DB_USER"],
                            password=password, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (os.environ["DB_NAME"],))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{os.environ["DB_NAME"]}"')
                log(msg="base créée", db=os.environ["DB_NAME"])
    except psycopg2.errors.DuplicateDatabase:
        pass  # créée au même moment par une autre instance
    finally:
        conn.close()


def connect():
    global pool
    password = ssm.get_parameter(Name=os.environ["DB_PASSWORD_PARAM"], WithDecryption=True)["Parameter"]["Value"]
    while True:
        try:
            pool = psycopg2.pool.ThreadedConnectionPool(
                1, POOL_SIZE, host=os.environ["DB_HOST"], dbname=os.environ["DB_NAME"],
                user=os.environ["DB_USER"], password=password, connect_timeout=5)
            break
        except psycopg2.OperationalError as e:
            if "does not exist" in str(e):
                create_database(password)
                continue
            log(msg="base indisponible, nouvel essai dans 5 s", error=str(e))
            time.sleep(5)

    products = json.load(open(PRODUCTS_FILE, encoding="utf-8"))
    with db() as cur:
        # Verrou applicatif : plusieurs instances peuvent démarrer en même temps.
        cur.execute("SELECT pg_advisory_xact_lock(42)")
        cur.execute(SCHEMA)
        for p in products:
            cur.execute(
                """INSERT INTO products (id, category, name, description, price, color, hex, sizes, stock)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                (p["id"], p["category"], p["name"], p["description"], p["price"], p["color"],
                 p["hex"], json.dumps(p["sizes"]), p["stock"]))
    log(msg="base initialisée", products=len(products))


class db:
    """Context manager : une transaction sur une connexion du pool."""

    def __enter__(self):
        if not pool_slots.acquire(timeout=POOL_WAIT_SECONDS):
            raise ApiError(503, "Service surchargé, réessayez dans quelques instants")
        try:
            self.conn = pool.getconn()
        except Exception:
            pool_slots.release()
            raise
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            pool.putconn(self.conn)
            pool_slots.release()


# ---------------------------------------------------------------------------
# Logique métier
# ---------------------------------------------------------------------------

def list_products(category):
    with db() as cur:
        if category:
            cur.execute("SELECT * FROM products WHERE category = %s ORDER BY id", (category,))
        else:
            cur.execute("SELECT * FROM products ORDER BY id")
        return cur.fetchall()


def get_product(product_id):
    with db() as cur:
        cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
        row = cur.fetchone()
    if not row:
        raise ApiError(404, "Produit introuvable")
    return row


def get_order(order_id):
    with db() as cur:
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        row = cur.fetchone()
    if not row:
        raise ApiError(404, "Commande introuvable")
    row["backend"] = SOURCE
    if row["shipping"] is None:
        del row["shipping"]
    return row


def parse_order(raw):
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
    lines = data.get("items") or []
    if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
        raise ApiError(400, f"La commande doit contenir entre 1 et {MAX_LINES} articles")
    merged = {}
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
    return {"name": name, "email": email}, merged


def create_order(raw):
    customer, lines = parse_order(raw)
    per_product = Counter()
    for (pid, _), qty in lines.items():
        per_product[pid] += qty

    order_id = "ORD-" + uuid.uuid4().hex[:10].upper()
    created_at = now_iso()
    with db() as cur:
        # FOR UPDATE : verrouille les lignes produit pendant le contrôle du stock.
        cur.execute("SELECT * FROM products WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (list(per_product),))
        catalog = {p["id"]: p for p in cur.fetchall()}

        items, total = [], Decimal("0")
        for (pid, size), qty in lines.items():
            product = catalog.get(pid)
            if not product:
                raise ApiError(400, f"Produit inconnu : {pid}")
            if size not in product["sizes"]:
                raise ApiError(400, f"Taille {size} indisponible pour {product['name']}")
            total += product["price"] * qty
            items.append({"product_id": pid, "name": product["name"], "size": size,
                          "quantity": qty, "unit_price": product["price"]})

        short = [pid for pid, qty in per_product.items() if catalog[pid]["stock"] < qty]
        if short:
            raise ApiError(409, "Stock insuffisant pour : " + ", ".join(short))
        # La version augmente à chaque modification : la projection DynamoDB (hybride)
        # ignore ainsi un événement rejoué ou arrivé dans le désordre.
        stock = []
        for pid, qty in per_product.items():
            cur.execute("UPDATE products SET stock = stock - %s, version = version + 1 WHERE id = %s "
                        "RETURNING id, stock, version", (qty, pid))
            row = cur.fetchone()
            stock.append({"product_id": row["id"], "stock": row["stock"], "version": row["version"]})

        history = [{"status": "CONFIRMED", "at": created_at}]
        cur.execute(
            """INSERT INTO orders (order_id, status, customer, items, total, created_at, updated_at, history)
               VALUES (%s, 'CONFIRMED', %s, %s, %s, %s, %s, %s)""",
            (order_id, json.dumps(customer), json.dumps(items, default=to_json), total,
             created_at, created_at, json.dumps(history)))

    order = {"order_id": order_id, "status": "CONFIRMED", "customer": customer, "items": items,
             "total": total, "created_at": created_at, "updated_at": created_at,
             "history": history, "backend": SOURCE}
    publish("ORDER_CREATED", order, stock)
    return order


def publish(event_type, order, stock=None):
    event = {"event_id": str(uuid.uuid4()), "event_type": event_type, "occurred_at": now_iso(),
             "source": SOURCE, "order": order}
    if stock is not None:
        event["stock"] = stock
    message = {"default": json.dumps(event, default=to_json)}
    if event_type == "ORDER_CREATED":
        lines = "\n".join(f"  - {i['quantity']} x {i['name']} (taille {i['size']})" for i in order["items"])
        message["email"] = (f"Bonjour {order['customer']['name']},\n\nVotre commande {order['order_id']} "
                            f"est confirmée.\n\n{lines}\n\nTotal : {order['total']} EUR\n\nMerci pour votre achat !")
        subject = f"Commande {order['order_id']} : confirmation"
    elif event_type == "ORDER_SHIPPED":
        ship = order["shipping"]
        message["email"] = (f"Bonjour {order['customer']['name']},\n\nVotre commande {order['order_id']} a été "
                            f"expédiée avec {ship['carrier']}.\nNuméro de suivi : {ship['tracking_number']}\n\nÀ bientôt !")
        subject = f"Commande {order['order_id']} : suivi de livraison"
    else:
        subject = f"Commande {order['order_id']}"
    sns.publish(
        TopicArn=TOPIC_ARN, MessageStructure="json", Message=json.dumps(message), Subject=subject,
        MessageAttributes={
            "event_type": {"DataType": "String", "StringValue": event_type},
            "source": {"DataType": "String", "StringValue": SOURCE},
        })


def advance(event):
    transition = TRANSITIONS.get(event["event_type"])
    if not transition:
        return
    expected, new_status, next_event = transition
    order_id = event["order"]["order_id"]
    now = now_iso()
    shipping = None
    if new_status == "SHIPPED":
        shipping = {"carrier": random.choice(CARRIERS), "tracking_number": "FR" + uuid.uuid4().hex[:12].upper()}
    with db() as cur:
        # Mise à jour conditionnelle => la transition n'est jamais appliquée deux fois.
        cur.execute(
            """UPDATE orders SET status = %s, updated_at = %s,
                      history = history || %s::jsonb, shipping = COALESCE(%s::jsonb, shipping)
               WHERE order_id = %s AND status = %s RETURNING *""",
            (new_status, now, json.dumps([{"status": new_status, "at": now}]),
             json.dumps(shipping) if shipping else None, order_id, expected))
        order = cur.fetchone()
        republish = order is None
        if republish:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
    # Statut déjà à jour : un essai précédent a pu échouer après la mise à jour, en
    # publiant sur SNS. On republie l'événement suivant, sinon la commande reste bloquée.
    if not order or (republish and order["status"] != new_status):
        log(msg="déjà traité, ignoré", order_id=order_id, event=event["event_type"])
        return
    log(msg="statut déjà à jour, événement republié" if republish else "statut mis à jour",
        order_id=order_id, status=new_status)
    order["backend"] = SOURCE
    publish(next_event, order)


# ---------------------------------------------------------------------------
# Workers SQS
# ---------------------------------------------------------------------------

def poll_forever(queue_url, handle):
    while True:
        try:
            resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=20)
            for msg in resp.get("Messages", []):
                try:
                    handle(json.loads(msg["Body"]))
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
                except Exception as e:  # noqa: BLE001 - rejoué par SQS puis envoyé en DLQ
                    log(msg="échec du traitement", error=str(e), queue=queue_url)
        except Exception as e:  # noqa: BLE001
            log(msg="erreur SQS", error=str(e))
            time.sleep(5)


# ---------------------------------------------------------------------------
# Kafka (MSK) - optionnel
# ---------------------------------------------------------------------------

stats_lock = threading.Lock()
stats = {"events": Counter(), "revenue": Decimal("0"), "products": Counter(), "recent": deque(maxlen=10)}


def start_kafka():
    from kafka import KafkaConsumer, KafkaProducer

    producer = KafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
                             value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                             key_serializer=lambda k: k.encode("utf-8"), acks="all")

    def bridge(event):
        # Clé = numéro de commande : tous les événements d'une commande vont dans la même partition (ordre garanti).
        producer.send(KAFKA_TOPIC, key=event["order"]["order_id"], value=event).get(timeout=10)

    def consume():
        consumer = KafkaConsumer(KAFKA_TOPIC, bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
                                 group_id=f"stats-{socket.gethostname()}", auto_offset_reset="earliest",
                                 value_deserializer=lambda v: json.loads(v.decode("utf-8")))
        for record in consumer:
            event = record.value
            with stats_lock:
                stats["events"][event["event_type"]] += 1
                if event["event_type"] == "ORDER_CREATED":
                    stats["revenue"] += Decimal(str(event["order"]["total"]))
                    for item in event["order"]["items"]:
                        stats["products"][item["name"]] += item["quantity"]
                stats["recent"].appendleft({"type": event["event_type"], "order_id": event["order"]["order_id"],
                                            "source": event["source"], "at": event["occurred_at"]})

    threading.Thread(target=poll_forever, args=(KAFKA_QUEUE_URL, bridge), daemon=True).start()
    threading.Thread(target=consume, daemon=True).start()
    log(msg="Kafka activé", bootstrap=KAFKA_BOOTSTRAP)


def get_stats():
    if not KAFKA_BOOTSTRAP:
        return {"enabled": False}
    with stats_lock:
        return {"enabled": True, "orders": stats["events"]["ORDER_CREATED"],
                "shipped": stats["events"]["ORDER_SHIPPED"], "revenue": stats["revenue"],
                "top_products": stats["products"].most_common(3), "recent": list(stats["recent"])}


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ShopVM/1.0"

    def send_json(self, status, body):
        data = json.dumps(body, default=to_json, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, path):
        target = (STATIC_DIR / (path.lstrip("/") or "index.html")).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            target = STATIC_DIR / "index.html"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self, method):
        path, _, query = self.path.partition("?")
        path = path.rstrip("/") or "/"
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "Méthode non autorisée")
                return self.send_static(path)
            if method == "GET" and path == "/api/health":
                return self.send_json(200, {"status": "ok", "backend": SOURCE, "region": REGION,
                                            "instance": socket.gethostname()})
            if method == "GET" and path == "/api/products":
                return self.send_json(200, {"products": list_products(params.get("category"))})
            if method == "GET" and path.startswith("/api/products/"):
                return self.send_json(200, get_product(path.split("/")[-1]))
            if method == "POST" and path == "/api/orders":
                length = int(self.headers.get("Content-Length") or 0)
                if length > 64 * 1024:
                    raise ApiError(413, "Requête trop volumineuse")
                return self.send_json(201, create_order(self.rfile.read(length).decode("utf-8")))
            if method == "GET" and path.startswith("/api/orders/"):
                return self.send_json(200, get_order(path.split("/")[-1]))
            if method == "GET" and path == "/api/stats":
                return self.send_json(200, get_stats())
            raise ApiError(404, "Route inconnue")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            log(msg="erreur interne", error=str(e), path=self.path)
            self.send_json(500, {"error": "Erreur interne"})

    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def log_message(self, fmt, *args):
        if "/api/health" not in self.path:
            log(msg="http", client=self.client_address[0], request=fmt % args)


if __name__ == "__main__":
    connect()
    if QUEUE_URL:
        threading.Thread(target=poll_forever, args=(QUEUE_URL, advance), daemon=True).start()
    if KAFKA_BOOTSTRAP:
        start_kafka()
    log(msg="serveur démarré", port=PORT, source=SOURCE)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
