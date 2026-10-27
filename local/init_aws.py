"""Crée dans LocalStack l'équivalent de ce que Terraform crée sur AWS, pour les trois solutions.

- SSM : mot de passe de la base (lu par les services EC2 simulés) ;
- SNS + SQS : même topic, mêmes files, mêmes filtres et délais que messaging.tf ;
- S3 : bucket d'audit ;
- DynamoDB : tables des solutions serverless et hybride, catalogue chargé depuis
  data/products.json ;
- Lambda : les six fonctions de lambda.tf, avec le code de src/lambda/ tel quel
  (copié dans l'image, pas dupliqué dans le dépôt), leurs déclencheurs SQS et les
  Function URL de "api" et "hybrid-read".

Enfin, écrit pour nginx (le CloudFront local) le nom d'hôte de chaque Function URL.
Relancé sur un LocalStack déjà initialisé, le script ne recrée rien et ne remet pas
le stock à zéro (pour cela : docker compose ... down, puis up).
"""

import io
import json
import os
import time
import urllib.request
import zipfile
from decimal import Decimal
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

ENDPOINT = os.environ["AWS_ENDPOINT_URL"]
DELAY = os.environ.get("FULFILLMENT_DELAY_SECONDS", "5")
ORIGIN_SECRET = os.environ["ORIGIN_SECRET"]
LAMBDA_DIR = "/opt/lambda"
PRODUCTS_FILE = "/opt/shop/products.json"
NGINX_VARS = os.environ.get("NGINX_VARS", "/etc/nginx/generated/function_urls.conf")
PREFIX = "threadly"
ROLE = "arn:aws:iam::000000000000:role/threadly-lambda"  # LocalStack ne vérifie pas les droits IAM

# Attend que LocalStack ait démarré les services utilisés.
for _ in range(90):
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            services = json.load(r)["services"]
        if all(services.get(s) in ("available", "running") for s in ("lambda", "dynamodb", "sns", "sqs", "s3", "ssm")):
            break
    except Exception:  # noqa: BLE001 - pas encore prêt
        pass
    time.sleep(1)
else:
    raise SystemExit("LocalStack n'a pas démarré")

ssm, sns, sqs, s3 = (boto3.client(s) for s in ("ssm", "sns", "sqs", "s3"))
ddb, lam = boto3.client("dynamodb"), boto3.client("lambda")

ssm.put_parameter(Name="/threadly-local/db/password", Value="local-password", Type="SecureString", Overwrite=True)
topic = sns.create_topic(Name=f"{PREFIX}-order-events")["TopicArn"]

# --- SNS -> SQS (messaging.tf) -----------------------------------------------
QUEUES = {
    "processing": (DELAY, {"source": ["serverless"], "event_type": ["ORDER_CREATED", "ORDER_PACKED"]}),
    "vm-processing": (DELAY, {"source": ["vm"], "event_type": ["ORDER_CREATED", "ORDER_PACKED"]}),
    "hybrid-projection": ("0", {"source": ["hybrid"], "event_type": ["ORDER_CREATED"]}),
    "hybrid-processing": (DELAY, {"source": ["hybrid"], "event_type": ["ORDER_CREATED", "ORDER_PACKED"]}),
    "audit": ("0", None),
}
queue_arn = {}
existing_subs = {s["Endpoint"] for s in sns.list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"]}
for name, (delay, policy) in QUEUES.items():
    dlq = sqs.create_queue(QueueName=f"{PREFIX}-{name}-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    url = sqs.create_queue(QueueName=f"{PREFIX}-{name}", Attributes={
        "DelaySeconds": delay, "VisibilityTimeout": "60",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 3}),
    })["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    queue_arn[name] = arn
    if arn not in existing_subs:
        attrs = {"RawMessageDelivery": "true"}
        if policy:
            attrs["FilterPolicy"] = json.dumps(policy)
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=arn, Attributes=attrs)
    print("file :", url)

# --- S3 et DynamoDB (storage.tf, database.tf) ---------------------------------
audit_bucket = f"{PREFIX}-local-audit"
try:
    s3.create_bucket(Bucket=audit_bucket, CreateBucketConfiguration={"LocationConstraint": "eu-west-3"})
except ClientError as e:
    if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
        raise

TABLES = {"products": "id", "orders": "order_id", "hybrid-products": "id", "hybrid-orders": "order_id"}
for name, key in TABLES.items():
    try:
        ddb.create_table(TableName=f"{PREFIX}-{name}", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}])
    except ddb.exceptions.ResourceInUseException:
        pass
    ddb.get_waiter("table_exists").wait(TableName=f"{PREFIX}-{name}")

products = json.load(open(PRODUCTS_FILE, encoding="utf-8"), parse_float=Decimal)
for table in ("products", "hybrid-products"):
    t = boto3.resource("dynamodb").Table(f"{PREFIX}-{table}")
    for p in products:
        try:
            # Seulement si absent : le stock (et, en hybride, sa version) n'est jamais écrasé.
            t.put_item(Item=p, ConditionExpression="attribute_not_exists(id)")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
print("tables DynamoDB prêtes,", len(products), "produits")

# --- Lambda (lambda.tf) --------------------------------------------------------
T = {k: f"{PREFIX}-{k}" for k in TABLES}
LAMBDAS = {
    "api": ("api", 10, 256, {"PRODUCTS_TABLE": T["products"], "ORDERS_TABLE": T["orders"],
                             "TOPIC_ARN": topic, "ORIGIN_SECRET": ORIGIN_SECRET}),
    "processor": ("processor", 30, 128, {"ORDERS_TABLE": T["orders"], "TOPIC_ARN": topic}),
    "audit": ("audit", 30, 128, {"AUDIT_BUCKET": audit_bucket}),
    "hybrid-read": ("api", 10, 256, {"PRODUCTS_TABLE": T["hybrid-products"], "ORDERS_TABLE": T["hybrid-orders"],
                                     "ORIGIN_SECRET": ORIGIN_SECRET, "BACKEND": "hybrid", "READ_ONLY": "1"}),
    "hybrid-processor": ("processor", 30, 128, {"ORDERS_TABLE": T["hybrid-orders"], "TOPIC_ARN": topic, "SOURCE": "hybrid"}),
    "hybrid-projector": ("projector", 30, 128, {"PRODUCTS_TABLE": T["hybrid-products"], "ORDERS_TABLE": T["hybrid-orders"]}),
}
TRIGGERS = {"processor": "processing", "audit": "audit",
            "hybrid-processor": "hybrid-processing", "hybrid-projector": "hybrid-projection"}


def zipped(code):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(f"{LAMBDA_DIR}/{code}/handler.py", "handler.py")
    return buf.getvalue()


for name, (code, timeout, memory, env) in LAMBDAS.items():
    fn = f"{PREFIX}-{name}"
    try:
        lam.create_function(FunctionName=fn, Runtime="python3.12", Handler="handler.handler", Role=ROLE,
                            Code={"ZipFile": zipped(code)}, Timeout=timeout, MemorySize=memory,
                            Environment={"Variables": env})
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=fn, ZipFile=zipped(code))
    lam.get_waiter("function_active_v2").wait(FunctionName=fn)
    print("lambda :", fn)

for name, queue in TRIGGERS.items():
    fn = f"{PREFIX}-{name}"
    if not lam.list_event_source_mappings(FunctionName=fn, EventSourceArn=queue_arn[queue])["EventSourceMappings"]:
        # Fenêtre de regroupement à 0, comme sur AWS (sinon ~20 s de latence, voir le rapport §7.2).
        lam.create_event_source_mapping(FunctionName=fn, EventSourceArn=queue_arn[queue], BatchSize=10,
                                        MaximumBatchingWindowInSeconds=0,
                                        FunctionResponseTypes=["ReportBatchItemFailures"])

# --- Function URL : nginx les appelle par leur nom d'hôte (comme CloudFront) ----
hosts = {}
for name in ("api", "hybrid-read"):
    fn = f"{PREFIX}-{name}"
    try:
        url = lam.create_function_url_config(FunctionName=fn, AuthType="NONE")["FunctionUrl"]
    except lam.exceptions.ResourceConflictException:
        url = lam.get_function_url_config(FunctionName=fn)["FunctionUrl"]
    hosts[name] = urlparse(url).hostname

os.makedirs(os.path.dirname(NGINX_VARS), exist_ok=True)
with open(NGINX_VARS, "w") as f:
    f.write("# Généré par local/init_aws.py : hôtes des Function URL dans LocalStack, secret d'origine.\n")
    f.write(f"map $server_port $function_url_host {{\n    8081 {hosts['api']};\n    8082 {hosts['hybrid-read']};\n}}\n")
    f.write(f'map $server_port $origin_secret {{\n    default "{ORIGIN_SECRET}";\n}}\n')
print("Function URL :", hosts)
print("topic :", topic)
