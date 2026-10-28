"""Boutique en ligne - solutions VM et hybride (EC2 + RDS PostgreSQL).

Lancé par systemd sur chaque instance de l'Auto Scaling Group, une fois par solution :
  - SHOP_SOURCE=vm (port 8080) : serveur HTTP (site statique + API commune), worker
    SQS "vm-processing" qui fait tout le traitement d'une commande payée (emballage,
    expédition et transporteur, transit, livraison ; une étape par message, le délai
    de la file simulant le temps passé) et le réapprovisionnement fournisseur (bon de
    commande sur STOCK_LOW, réception après le délai) et le rapport quotidien (message
    déposé par EventBridge Scheduler), sans Step Functions ni Lambda, et, si MSK est
    activé, pont SQS -> Kafka et stats temps réel ;
  - SHOP_SOURCE=hybrid (port 8081, base shop_hybrid) : CloudFront lui envoie toutes
    les écritures (commande, paiement, annulation, retours, admin). Son worker SQS
    "hybrid-restock" fait le réapprovisionnement (écritures de stock dans RDS) ; le
    traitement des commandes est fait par Step Functions et des Lambdas.
Sur les deux, une purge périodique libère les réservations expirées et traite les
commandes impayées (relecture du paiement chez Stripe avant toute annulation).
Les routes, les règles et l'accès aux données sont dans le paquet commun shop
(shop/api.py, shop/sql_store.py). Chaque ORDER_CREATED porte le stock après la
commande (avec sa version), pour la projection du catalogue dans DynamoDB (hybride).
"""

import json
import mimetypes
import os
import socket
import sys
import threading
import time
from collections import Counter, deque
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

import boto3

# Le paquet shop est installé à côté de app.py (EC2 : /opt/shop/shop).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shop import api, domain  # noqa: E402
from shop.auth import Authenticator  # noqa: E402
from shop.files import ImageFiles, S3Files  # noqa: E402
from shop.payments import StripePayments, public_config_from_env  # noqa: E402
from shop.sql_store import SqlStore, log  # noqa: E402

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
# Hybride : table DynamoDB où fait foi le statut des commandes (annulation).
STATUS_TABLE = os.environ.get("STATUS_TABLE", "")
SWEEP_SECONDS = 30

sns = boto3.client("sns", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)

store = None  # SqlStore, créé au démarrage (le mot de passe est lu dans SSM)


# ---------------------------------------------------------------------------
# Worker SQS : commandes (VM) ORDER_PAID -> PACKED -> SHIPPED -> IN_TRANSIT -> DELIVERED ;
# réappro (VM, hybride) STOCK_LOW -> bon de commande -> PURCHASE_ORDER_CREATED (reçu
# après le délai de la file) -> livraison fournisseur
# ---------------------------------------------------------------------------

def handle_event(event):
    kind = event["event_type"]
    if kind == "STOCK_LOW":
        po = api.supplier_order(store, ctx, event["alert"])
        log(msg="bon de commande créé" if po else "bon déjà ouvert, alerte ignorée",
            product_id=event["alert"]["product_id"], po_id=po and po["po_id"])
    elif kind == "PURCHASE_ORDER_CREATED":
        po = api.supplier_receive(store, ctx, event["purchase_orders"][0]["po_id"])
        log(msg="livraison fournisseur reçue", po_id=po["po_id"], quantity=po["quantity"])
    elif kind == "DAILY_REPORT_REQUESTED":  # EventBridge Scheduler (lot 4), VM seulement
        report = api.daily_report(store, ctx)
        log(msg="rapport quotidien produit", key=report["key"], sold_24h=report["sold_24h"])
    elif kind in domain.TRANSITIONS:
        advance(event)


def advance(event):
    result = store.advance(event)
    order_id = event["order"]["order_id"]
    if result is None:
        log(msg="déjà traité, ignoré", order_id=order_id, event=event["event_type"])
        return
    order, next_event, replayed = result
    log(msg="statut déjà à jour, événement republié" if replayed else "statut mis à jour",
        order_id=order_id, status=order["status"])
    domain.publish(sns, TOPIC_ARN, next_event, order, SOURCE)


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
        key = (event.get("order") or {}).get("order_id") or event["event_id"]
        producer.send(KAFKA_TOPIC, key=key, value=event).get(timeout=10)

    def consume():
        consumer = KafkaConsumer(KAFKA_TOPIC, bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
                                 group_id=f"stats-{socket.gethostname()}", auto_offset_reset="earliest",
                                 value_deserializer=lambda v: json.loads(v.decode("utf-8")))
        for record in consumer:
            event = record.value
            if not event.get("order"):  # événements de stock : pas dans les stats de commandes
                continue
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


ctx = api.Context(
    backend=SOURCE,
    region=REGION,
    publish=lambda event_type, **payload: domain.publish_event(sns, TOPIC_ARN, event_type, SOURCE, **payload),
    health_extra={"instance": socket.gethostname()},
    stats=get_stats,
    auth=Authenticator.from_env(),
    files=S3Files(os.environ["DATA_BUCKET"]) if os.environ.get("DATA_BUCKET") else None,
    images=ImageFiles(os.environ["IMAGES_BUCKET"]) if os.environ.get("IMAGES_BUCKET") else None,
    images_url=os.environ.get("IMAGES_URL", ""),
    realtime_url=os.environ.get("REALTIME_URL", ""),
    payments=StripePayments.from_env(ssm),
    payment_config=public_config_from_env(),
    fulfillment="ec2-worker" if SOURCE == "vm" else "step-functions",
)


def sweep_forever():
    """Libère les réservations expirées (plusieurs instances : SKIP LOCKED, pas de doublon)
    et traite les commandes impayées (transitions conditionnelles : une seule instance
    confirme ou annule une commande)."""
    while True:
        time.sleep(SWEEP_SECONDS)
        try:
            result = store.expire_reservations()
            if result["stock"]:
                api.publish_changes(store, ctx, "STOCK_CHANGED", result)
                log(msg="réservations expirées libérées", lignes_de_stock=len(result["stock"]))
        except Exception as e:  # noqa: BLE001
            log(msg="échec de la purge des réservations", error=str(e))
        try:
            unpaid = api.expire_unpaid(store, ctx)
            if unpaid["confirmed"] or unpaid["cancelled"]:
                log(msg="commandes impayées traitées", **unpaid)
        except Exception as e:  # noqa: BLE001
            log(msg="échec de la purge des paiements", error=str(e))


# ---------------------------------------------------------------------------
# Serveur HTTP : site statique + API commune
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ShopVM/1.0"

    def send_json(self, status, body):
        data = domain.dumps(body).encode("utf-8")
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
        if not path.startswith("/api/"):
            if method not in ("GET", "HEAD"):
                return self.send_json(405, {"error": "Méthode non autorisée"})
            return self.send_static(path.rstrip("/") or "/")
        params = dict(parse_qsl(query))  # décodé (%C3%A8 -> è), comme les paramètres reçus par la Lambda
        length = int(self.headers.get("Content-Length") or 0)
        if length > domain.MAX_BODY_BYTES:
            return self.send_json(413, {"error": "Requête trop volumineuse"})
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self.send_json(*api.handle(store, ctx, method, path, params, body, dict(self.headers)))

    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("HEAD")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def log_message(self, fmt, *args):
        if "/api/health" not in self.path:
            log(msg="http", client=self.client_address[0], request=fmt % args)


if __name__ == "__main__":
    password = ssm.get_parameter(Name=os.environ["DB_PASSWORD_PARAM"], WithDecryption=True)["Parameter"]["Value"]
    status_store = None
    if STATUS_TABLE:
        from shop.dynamo_store import DynamoStore
        status_store = DynamoStore(STATUS_TABLE, backend=SOURCE)
    store = SqlStore(os.environ["DB_HOST"], os.environ["DB_NAME"], os.environ["DB_USER"], password,
                     backend=SOURCE, status_store=status_store)
    store.connect(json.load(open(PRODUCTS_FILE, encoding="utf-8")))
    if QUEUE_URL:
        threading.Thread(target=poll_forever, args=(QUEUE_URL, handle_event), daemon=True).start()
    threading.Thread(target=sweep_forever, daemon=True).start()
    if KAFKA_BOOTSTRAP:
        start_kafka()
    log(msg="serveur démarré", port=PORT, source=SOURCE)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
