"""Pousse les événements aux navigateurs connectés (API Gateway WebSocket).

Déclenchée par la file SQS "realtime" (abonnée au topic SNS, toutes solutions ; la
source de l'événement choisit les connexions de la bonne boutique) :
  ORDER_CREATED / PACKED / SHIPPED / CANCELLED -> connexions qui suivent la commande
  BACK_IN_STOCK (notifications)                 -> connexions du client concerné
Une connexion fermée (GoneException) est supprimée de la table. Le front garde un
polling de secours : un message perdu ne bloque jamais le suivi.
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Key

from shop import domain

table = boto3.resource("dynamodb").Table(os.environ["REALTIME_TABLE"])
gateway = boto3.client("apigatewaymanagementapi", endpoint_url=os.environ["CONNECTIONS_URL"])


def connections(pk):
    items = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("CONN#"))["Items"]
    return [i["sk"][5:] for i in items]


def push(pk, message):
    sent = 0
    for conn in connections(pk):
        try:
            gateway.post_to_connection(ConnectionId=conn, Data=json.dumps(message, default=domain.to_json).encode())
            sent += 1
        except gateway.exceptions.GoneException:
            table.delete_item(Key={"pk": pk, "sk": f"CONN#{conn}"})
    return sent


def handle(event):
    kind, shop = event["event_type"], event["source"]
    if kind.startswith("ORDER_") and event.get("order"):
        order = event["order"]
        return push(f"ORDER#{shop}#{order['order_id']}", {"type": "order", "event": kind, "order": order})
    if kind == "BACK_IN_STOCK":
        return sum(push(f"USER#{shop}#{n['user_id']}", {"type": "notification", "notification": n})
                   for n in event.get("notifications") or [])
    return 0


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            sent = handle(body)
            print(json.dumps({"msg": "poussé", "event": body["event_type"], "connexions": sent}))
        except Exception as e:  # noqa: BLE001 - rejoué puis envoyé en DLQ
            print(json.dumps({"msg": "échec", "error": str(e), "message_id": record["messageId"]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
