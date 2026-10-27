"""Projection CQRS de la solution hybride, déclenchée par la file SQS "hybrid-projection".

RDS (EC2 hybride) fait foi pour le stock et la création des commandes. Chaque
ORDER_CREATED est recopié dans DynamoDB, que lit la Lambda hybrid-read :
  - la commande est créée dans la table orders (si elle n'y est pas déjà) ;
  - le stock de chaque produit est remplacé par la valeur de l'événement, seulement
    si sa version est plus récente : un événement rejoué ou arrivé dans le
    désordre ne fait jamais revenir le stock en arrière.
Le statut de la commande est ensuite géré dans DynamoDB par la Lambda hybrid-processor.
"""

import json
import os
import time
from datetime import datetime
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
products_table = dynamodb.Table(os.environ["PRODUCTS_TABLE"])
orders_table = dynamodb.Table(os.environ["ORDERS_TABLE"])


def project(event, delays):
    if event["event_type"] != "ORDER_CREATED":
        return
    # parse_float=Decimal : DynamoDB refuse les float.
    order = json.loads(json.dumps(event["order"]), parse_float=Decimal)
    try:
        orders_table.put_item(Item=order, ConditionExpression="attribute_not_exists(order_id)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    for level in event.get("stock", []):
        try:
            products_table.update_item(
                Key={"id": level["product_id"]},
                UpdateExpression="SET stock = :s, stock_version = :v",
                ConditionExpression="attribute_exists(id) AND (attribute_not_exists(stock_version) OR stock_version < :v)",
                ExpressionAttributeValues={":s": level["stock"], ":v": level["version"]},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            print(json.dumps({"msg": "version déjà appliquée ou plus ancienne, ignorée", **level}))
    print(json.dumps({"msg": "projeté", "order_id": order["order_id"], "stock": event.get("stock", []), **delays}))


def delays_ms(record):
    """Où passe le temps : SNS -> SQS (sent), attente dans SQS (queue), Lambda (total)."""
    attrs = record.get("attributes", {})
    sent = int(attrs.get("SentTimestamp", 0))
    received = int(attrs.get("ApproximateFirstReceiveTimestamp", 0))
    return {"sqs_wait_ms": received - sent if sent and received else None,
            "total_since_sqs_ms": int(time.time() * 1000) - sent if sent else None}


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            delays = delays_ms(record)
            # Publication (EC2) -> arrivée dans SQS, à la seconde près (occurred_at est en secondes).
            sent = int(record.get("attributes", {}).get("SentTimestamp", 0))
            if sent and body.get("occurred_at"):
                delays["sns_to_sqs_ms"] = int(sent - datetime.fromisoformat(body["occurred_at"]).timestamp() * 1000)
            project(body, delays)
        except Exception as e:  # noqa: BLE001 - le message sera rejoué puis envoyé en DLQ
            print(json.dumps({"msg": "échec", "error": str(e), "message_id": record["messageId"]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
