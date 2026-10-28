"""Crée dans LocalStack l'équivalent de ce que Terraform crée sur AWS, pour les trois solutions.

- SSM : mot de passe de la base (lu par les services EC2 simulés) ;
- SNS + SQS : même topic, mêmes files, mêmes filtres et délais que messaging.tf ;
- S3 : bucket d'audit ;
- DynamoDB : tables des solutions serverless et hybride, catalogue chargé depuis
  data/products.json ;
- S3 : bucket d'images, notification vers la Lambda thumbnailer (media.tf) ;
- Step Functions : workflow de traitement des commandes (serverless et hybride, même
  définition que infra/templates/fulfillment.asl.json) et de réapprovisionnement
  (serverless, restock.asl.json) ; SES : expéditeur vérifié ;
- Lambda : les fonctions de lambda.tf (sauf realtime et notifier : pas de WebSocket
  en local), avec le code de src/lambda/ tel quel (copié dans l'image, pas dupliqué
  dans le dépôt), leurs déclencheurs SQS et les Function URL de "api" et "hybrid-read".

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
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

from shop.dynamo_store import seed_items  # même catalogue initial que Terraform

ENDPOINT = os.environ["AWS_ENDPOINT_URL"]
DELAY = os.environ.get("FULFILLMENT_DELAY_SECONDS", "5")
ORIGIN_SECRET = os.environ["ORIGIN_SECRET"]
S3_PUBLIC_ENDPOINT = os.environ.get("S3_PUBLIC_ENDPOINT", "http://localhost:4566")
LAMBDA_DIR = "/opt/lambda"
SHOP_DIR = "/opt/shop/shop"
PRODUCTS_FILE = "/opt/shop/products.json"
NGINX_VARS = os.environ.get("NGINX_VARS", "/etc/nginx/generated/function_urls.conf")
PREFIX = "threadly"
ROLE = "arn:aws:iam::000000000000:role/threadly-lambda"  # LocalStack ne vérifie pas les droits IAM

# Attend que LocalStack ait démarré les services utilisés.
for _ in range(90):
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            services = json.load(r)["services"]
        if all(services.get(s) in ("available", "running")
               for s in ("lambda", "dynamodb", "sns", "sqs", "s3", "ssm", "stepfunctions", "ses")):
            break
    except Exception:  # noqa: BLE001 - pas encore prêt
        pass
    time.sleep(1)
else:
    raise SystemExit("LocalStack n'a pas démarré")

ssm, sns, sqs, s3 = (boto3.client(s) for s in ("ssm", "sns", "sqs", "s3"))
ddb, lam = boto3.client("dynamodb"), boto3.client("lambda")

ssm.put_parameter(Name="/threadly-local/db/password", Value="local-password", Type="SecureString", Overwrite=True)
# Clé secrète du faux Stripe local (local/stripe_local.py), lue comme sur AWS dans SSM.
ssm.put_parameter(Name=os.environ["STRIPE_SECRET_PARAM"], Value="sk_test_local", Type="SecureString", Overwrite=True)
MAIL_FROM = os.environ.get("MAIL_FROM", "boutique@threadly.local")
boto3.client("ses").verify_email_identity(EmailAddress=MAIL_FROM)
topic = sns.create_topic(Name=f"{PREFIX}-order-events")["TopicArn"]

# --- SNS -> SQS (messaging.tf) -----------------------------------------------
FULFILLMENT_STEPS = ["ORDER_PAID", "ORDER_PACKED", "ORDER_SHIPPED", "ORDER_IN_TRANSIT"]
SUPPLY_STEPS = ["STOCK_LOW", "PURCHASE_ORDER_CREATED"]
QUEUES = {
    # Serverless et hybride : la file ne fait que démarrer le workflow Step Functions.
    "processing": ("0", {"source": ["serverless"], "event_type": ["ORDER_PAID", "STOCK_LOW"]}),
    # VM : le worker EC2 fait chaque étape (et le réappro), le délai de la file simule le temps passé.
    "vm-processing": (DELAY, {"source": ["vm"], "event_type": FULFILLMENT_STEPS + SUPPLY_STEPS}),
    "hybrid-projection": ("0", {"source": ["hybrid"], "event_type": ["ORDER_CREATED", "ORDER_CANCELLED",
                                                                     "STOCK_CHANGED", "PRODUCT_CHANGED",
                                                                     "PROMO_CHANGED", "REVIEW_ADDED",
                                                                     "WISHLIST_CHANGED", "BACK_IN_STOCK",
                                                                     "ORDER_RETURNED", "PURCHASE_ORDER_CREATED",
                                                                     "PURCHASE_ORDER_RECEIVED"]}),
    "hybrid-processing": ("0", {"source": ["hybrid"], "event_type": ["ORDER_PAID"]}),
    # Hybride : réappro par l'EC2 (stock dans RDS).
    "hybrid-restock": (DELAY, {"source": ["hybrid"], "event_type": SUPPLY_STEPS}),
    "audit": ("0", None),
    "mailer": ("0", {"event_type": ["ORDER_PAID", "ORDER_SHIPPED", "ORDER_DELIVERED", "ORDER_CANCELLED",
                                    "ORDER_REFUNDED", "ORDER_RETURN_REQUESTED", "STOCK_LOW",
                                    "PURCHASE_ORDER_CREATED", "DAILY_REPORT", "BACK_IN_STOCK"]}),
}
queue_arn = {}
existing_subs = {s["Endpoint"]: s["SubscriptionArn"]
                 for s in sns.list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"]}
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
    elif policy:  # stack déjà initialisée : le filtre suit le code (nouveaux événements)
        sns.set_subscription_attributes(SubscriptionArn=existing_subs[arn], AttributeName="FilterPolicy",
                                        AttributeValue=json.dumps(policy))
    print("file :", url)

# --- S3 et DynamoDB (storage.tf, database.tf) ---------------------------------
audit_bucket, data_bucket = f"{PREFIX}-local-audit", f"{PREFIX}-local-data"
images_bucket = f"{PREFIX}-local-images"
IMAGES_URL = f"{S3_PUBLIC_ENDPOINT}/{images_bucket}/images"
for bucket in (audit_bucket, data_bucket, images_bucket):
    try:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "eu-west-3"})
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
# Même règle CORS que storage.tf : le back-office dépose le CSV par lien présigné.
s3.put_bucket_cors(Bucket=data_bucket, CORSConfiguration={"CORSRules": [
    {"AllowedMethods": ["GET", "PUT"], "AllowedOrigins": ["*"], "AllowedHeaders": ["Content-Type"],
     "MaxAgeSeconds": 300}]})
s3.put_bucket_cors(Bucket=images_bucket, CORSConfiguration={"CORSRules": [
    {"AllowedMethods": ["POST"], "AllowedOrigins": ["*"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 300}]})

# Une table par solution (single-table design, voir shop/dynamo_store.py), à la demande.
TABLES = {"shop": f"{PREFIX}-shop", "hybrid": f"{PREFIX}-hybrid"}
for name in TABLES.values():
    try:
        ddb.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("pk", "sk", "gsi1pk", "gsi1sk")],
            GlobalSecondaryIndexes=[{"IndexName": "gsi1", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"},
                                                   {"AttributeName": "gsi1sk", "KeyType": "RANGE"}]}])
        ddb.get_waiter("table_exists").wait(TableName=name)
        ddb.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
    except ddb.exceptions.ResourceInUseException:
        pass

products = json.load(open(PRODUCTS_FILE, encoding="utf-8"))
for name in TABLES.values():
    t = boto3.resource("dynamodb").Table(name)
    for item in seed_items(products):
        try:
            # Seulement si absent : le stock (et, en hybride, sa version) n'est jamais écrasé.
            t.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
print("tables DynamoDB prêtes,", len(products), "produits")

# --- Lambda (lambda.tf) --------------------------------------------------------
AUTH = {"AUTH_ISSUER": os.environ["AUTH_ISSUER"], "AUTH_CLIENT_ID": os.environ["AUTH_CLIENT_ID"],
        "AUTH_JWKS_URL": os.environ["AUTH_JWKS_URL"], "AUTH_PUBLIC_URL": os.environ["AUTH_PUBLIC_URL"],
        # Stack locale seulement : accepte le fournisseur d'identité local (voir shop/auth.py).
        "ALLOW_LOCAL_AUTH": "1"}
# Paiement : faux Stripe local (accepté seulement avec ALLOW_LOCAL_STRIPE=1, voir shop/payments.py).
STRIPE_PUBLIC = {k: os.environ[k] for k in ("STRIPE_PUBLISHABLE_KEY", "STRIPE_API_BASE", "STRIPE_PUBLIC_BASE",
                                            "ALLOW_LOCAL_STRIPE")}
STRIPE = {**STRIPE_PUBLIC, "STRIPE_SECRET_PARAM": os.environ["STRIPE_SECRET_PARAM"]}
COMMON = {**AUTH, **STRIPE, "DATA_BUCKET": data_bucket, "S3_PUBLIC_ENDPOINT": S3_PUBLIC_ENDPOINT,
          "IMAGES_BUCKET": images_bucket, "IMAGES_URL": IMAGES_URL}
SFN = "arn:aws:states:eu-west-3:000000000000:stateMachine"
# Machine d'état => (nom, Lambda appelée par ses étapes, définition).
MACHINES = {"serverless": (f"{PREFIX}-fulfillment", "fulfillment", "fulfillment.asl.json"),
            "hybrid": (f"{PREFIX}-hybrid-fulfillment", "hybrid-fulfillment", "fulfillment.asl.json"),
            "restock": (f"{PREFIX}-restock", "supplier", "restock.asl.json")}
LAMBDAS = {
    "api": ("api", 10, 256, {"TABLE": TABLES["shop"], "TOPIC_ARN": topic, "ORIGIN_SECRET": ORIGIN_SECRET,
                             "FULFILLMENT": "step-functions", **COMMON}),
    "processor": ("processor", 30, 128, {"TABLE": TABLES["shop"],
                                         "STATE_MACHINE_ARN": f"{SFN}:{MACHINES['serverless'][0]}",
                                         "RESTOCK_STATE_MACHINE_ARN": f"{SFN}:{MACHINES['restock'][0]}"}),
    "fulfillment": ("fulfillment", 30, 128, {"TABLE": TABLES["shop"], "TOPIC_ARN": topic}),
    "supplier": ("supplier", 30, 128, {"TABLE": TABLES["shop"], "TOPIC_ARN": topic}),
    "sweeper": ("sweeper", 60, 128, {"TABLE": TABLES["shop"], "TOPIC_ARN": topic, **STRIPE}),
    "audit": ("audit", 30, 128, {"AUDIT_BUCKET": audit_bucket}),
    "hybrid-read": ("api", 10, 256, {"TABLE": TABLES["hybrid"], "ORIGIN_SECRET": ORIGIN_SECRET,
                                     "BACKEND": "hybrid", "READ_ONLY": "1", "IMAGES_URL": IMAGES_URL,
                                     "FULFILLMENT": "step-functions", **AUTH, **STRIPE_PUBLIC}),
    "hybrid-processor": ("processor", 30, 128, {"TABLE": TABLES["hybrid"], "SOURCE": "hybrid",
                                                "STATE_MACHINE_ARN": f"{SFN}:{MACHINES['hybrid'][0]}"}),
    "hybrid-fulfillment": ("fulfillment", 30, 128, {"TABLE": TABLES["hybrid"], "TOPIC_ARN": topic,
                                                    "SOURCE": "hybrid"}),
    "hybrid-projector": ("projector", 30, 128, {"TABLE": TABLES["hybrid"]}),
    "thumbnailer": ("thumbnailer", 30, 512, {}),
    # Pas de bac à sable SES en local : les messages restent dans LocalStack (/_aws/ses).
    # Rapport quotidien (reporting.tf) : pas d'EventBridge Scheduler en local, invoqué à
    # la demande (scripts/daily_report.sh ou bouton du back-office).
    "reporter": ("reporter", 60, 256, {"TABLE": TABLES["shop"], "TOPIC_ARN": topic, "DATA_BUCKET": data_bucket,
                                       "S3_PUBLIC_ENDPOINT": S3_PUBLIC_ENDPOINT}),
    "hybrid-reporter": ("reporter", 60, 256, {"TABLE": TABLES["hybrid"], "SOURCE": "hybrid", "TOPIC_ARN": topic,
                                              "DATA_BUCKET": data_bucket, "S3_PUBLIC_ENDPOINT": S3_PUBLIC_ENDPOINT}),
    "mailer": ("mailer", 30, 128, {"MAIL_FROM": MAIL_FROM, "MAIL_ADMIN": MAIL_FROM,
                                   "MAIL_SKIP_DOMAINS": "example.com,example.org,example.net"}),
}
DEPS = {"thumbnailer": "/opt/lambda-deps/thumbnailer"}  # Pillow arm64 (voir Dockerfile)
TRIGGERS = {"processor": "processing", "audit": "audit", "mailer": "mailer",
            "hybrid-processor": "hybrid-processing", "hybrid-projector": "hybrid-projection"}


def zipped(code):
    """Même contenu que le zip de Terraform : handler + paquet commun shop/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(f"{LAMBDA_DIR}/{code}/handler.py", "handler.py")
        for name in sorted(os.listdir(SHOP_DIR)):
            if name.endswith(".py"):
                z.write(f"{SHOP_DIR}/{name}", f"shop/{name}")
        if code in DEPS:
            for root, _, files in os.walk(DEPS[code]):
                for name in files:
                    path = os.path.join(root, name)
                    z.write(path, os.path.relpath(path, DEPS[code]))
    return buf.getvalue()


for name, (code, timeout, memory, env) in LAMBDAS.items():
    fn = f"{PREFIX}-{name}"
    try:
        lam.create_function(FunctionName=fn, Runtime="python3.12", Handler="handler.handler", Role=ROLE,
                            Code={"ZipFile": zipped(code)}, Timeout=timeout, MemorySize=memory,
                            Environment={"Variables": env}, Architectures=["arm64"])
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=fn, ZipFile=zipped(code))
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
        lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env})
    lam.get_waiter("function_active_v2").wait(FunctionName=fn)
    print("lambda :", fn)

# Purge des réservations expirées (serverless) : règle EventBridge chaque minute.
events = boto3.client("events")
sweeper_arn = lam.get_function(FunctionName=f"{PREFIX}-sweeper")["Configuration"]["FunctionArn"]
rule_arn = events.put_rule(Name=f"{PREFIX}-sweeper", ScheduleExpression="rate(1 minute)")["RuleArn"]
events.put_targets(Rule=f"{PREFIX}-sweeper", Targets=[{"Id": "sweeper", "Arn": sweeper_arn}])
try:
    lam.add_permission(FunctionName=f"{PREFIX}-sweeper", StatementId="events", Action="lambda:InvokeFunction",
                       Principal="events.amazonaws.com", SourceArn=rule_arn)
except lam.exceptions.ResourceConflictException:
    pass

# Workflow des commandes (Step Functions) : même définition que Terraform.
sfn = boto3.client("stepfunctions")
for name, fn, template in MACHINES.values():
    fn_arn = lam.get_function(FunctionName=f"{PREFIX}-{fn}")["Configuration"]["FunctionArn"]
    definition = (open(f"/opt/shop/{template}", encoding="utf-8").read().replace("${fulfillment_arn}", fn_arn)
                  .replace("${supplier_arn}", fn_arn).replace("${delay}", DELAY))
    try:
        sfn.create_state_machine(name=name, definition=definition, roleArn=ROLE, type="STANDARD")
    except sfn.exceptions.StateMachineAlreadyExists:
        sfn.update_state_machine(stateMachineArn=f"{SFN}:{name}", definition=definition)
    print("state machine :", name)

# Dépôt d'un original -> miniatures (media.tf).
thumb_arn = lam.get_function(FunctionName=f"{PREFIX}-thumbnailer")["Configuration"]["FunctionArn"]
try:
    lam.add_permission(FunctionName=f"{PREFIX}-thumbnailer", StatementId="s3", Action="lambda:InvokeFunction",
                       Principal="s3.amazonaws.com", SourceArn=f"arn:aws:s3:::{images_bucket}")
except lam.exceptions.ResourceConflictException:
    pass
s3.put_bucket_notification_configuration(Bucket=images_bucket, NotificationConfiguration={
    "LambdaFunctionConfigurations": [{"LambdaFunctionArn": thumb_arn, "Events": ["s3:ObjectCreated:*"],
                                      "Filter": {"Key": {"FilterRules": [{"Name": "prefix",
                                                                          "Value": "originals/"}]}}}]})

for name, queue in TRIGGERS.items():
    fn = f"{PREFIX}-{name}"
    if not lam.list_event_source_mappings(FunctionName=fn, EventSourceArn=queue_arn[queue])["EventSourceMappings"]:
        # Fenêtre de regroupement à 0, comme sur AWS (sinon ~20 s de latence, voir le rapport, annexe E.2).
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
