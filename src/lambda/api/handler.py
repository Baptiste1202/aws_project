"""API de la boutique, exposée via une Lambda Function URL derrière CloudFront.

- Solution serverless : toutes les routes.
- Solution hybride (READ_ONLY=1, BACKEND=hybrid) : lectures seulement (catalogue,
  suivi, consultations d'admin). Les écritures passent par l'EC2 hybride ; la table
  est un modèle de lecture mis à jour par les événements.
Les routes et les règles sont dans le paquet commun shop (shop/api.py).
"""

import base64
import json
import os

import boto3

from shop import api, domain
from shop.auth import Authenticator
from shop.dynamo_store import DynamoStore
from shop.files import ImageFiles, S3Files
from shop.payments import StripePayments, public_config_from_env

ORIGIN_SECRET = os.environ["ORIGIN_SECRET"]
BACKEND = os.environ.get("BACKEND", "serverless")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")

store = DynamoStore(os.environ["TABLE"], backend=BACKEND)
sns = boto3.client("sns") if TOPIC_ARN else None

ctx = api.Context(
    backend=BACKEND,
    region=os.environ["AWS_REGION"],
    read_only=os.environ.get("READ_ONLY") == "1",
    publish=(lambda event_type, **payload: domain.publish_event(sns, TOPIC_ARN, event_type, BACKEND, **payload))
    if sns else None,
    auth=Authenticator.from_env(),
    files=S3Files(os.environ["DATA_BUCKET"]) if os.environ.get("DATA_BUCKET") else None,
    images=ImageFiles(os.environ["IMAGES_BUCKET"]) if os.environ.get("IMAGES_BUCKET") else None,
    images_url=os.environ.get("IMAGES_URL", ""),
    realtime_url=os.environ.get("REALTIME_URL", ""),
    # Lecture seule (hybride) : pas de clé secrète, seulement la configuration publique.
    payments=None if os.environ.get("READ_ONLY") == "1" else StripePayments.from_env(),
    payment_config=public_config_from_env(),
    fulfillment=os.environ.get("FULFILLMENT", ""),
)


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=domain.to_json, ensure_ascii=False),
    }


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("x-origin-verify") != ORIGIN_SECRET:
        return response(403, {"error": "Accès direct interdit, passez par CloudFront"})
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    status, payload = api.handle(store, ctx, event["requestContext"]["http"]["method"],
                                 event.get("rawPath", "/"), event.get("queryStringParameters") or {}, body,
                                 headers)
    return response(status, payload)
