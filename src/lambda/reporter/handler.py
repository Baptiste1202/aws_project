"""Rapport quotidien (lot 4), déclenché chaque matin par EventBridge Scheduler.

Solutions serverless (table shop) et hybride (SOURCE=hybrid : modèle de lecture
DynamoDB, à jour du stock, des commandes et de leur statut) : CSV des ventes des
dernières 24 h, du stock et de la valorisation par produit, déposé dans S3
(reports/<solution>/) puis envoyé par SES (événement DAILY_REPORT -> Lambda mailer).
La VM produit le même rapport sur ses EC2 (le Scheduler écrit dans sa file SQS).
Le code est dans shop/api.py (daily_report) et shop/domain.py.
"""

import json
import os

import boto3

from shop import api, domain
from shop.dynamo_store import DynamoStore
from shop.files import S3Files

SOURCE = os.environ.get("SOURCE", "serverless")
TOPIC_ARN = os.environ["TOPIC_ARN"]
store = DynamoStore(os.environ["TABLE"], backend=SOURCE)
sns = boto3.client("sns")

ctx = api.Context(backend=SOURCE, region=os.environ.get("AWS_REGION", ""),
                  publish=lambda event_type, **payload: domain.publish_event(sns, TOPIC_ARN, event_type, SOURCE,
                                                                             **payload),
                  files=S3Files(os.environ["DATA_BUCKET"]))


def handler(event, context):
    report = api.daily_report(store, ctx)
    print(json.dumps({"msg": "rapport quotidien produit", **{k: v for k, v in report.items() if k != "url"}},
                     default=domain.to_json))
    return {k: report[k] for k in ("key", "file", "products", "sold_24h")}
