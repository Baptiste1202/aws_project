"""Traitement interne des commandes, déclenché par une file SQS :
"processing" (solution serverless) ou "hybrid-processing" (solution hybride, SOURCE=hybrid).

Chaque étape est un événement SNS ; le délai de livraison de la file SQS simule
le temps passé en entrepôt :
  ORDER_CREATED -> emballage   -> statut PACKED,  publie ORDER_PACKED
  ORDER_PACKED  -> expédition  -> statut SHIPPED, publie ORDER_SHIPPED
Les mises à jour sont conditionnelles (statut attendu) : une transition n'est
jamais appliquée deux fois. Si le statut est déjà le statut cible (message
rejoué après un échec de publication SNS), l'événement suivant est republié.
En hybride, la commande vient de RDS par l'événement ORDER_CREATED, reçu à la fois
par la Lambda projector et par celle-ci : le premier des deux l'écrit dans DynamoDB
(écriture conditionnelle, idempotente), sans attendre l'autre.
"""

import json
import os
import random
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

orders_table = boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"])
sns = boto3.client("sns")
TOPIC_ARN = os.environ["TOPIC_ARN"]
SOURCE = os.environ.get("SOURCE", "serverless")

CARRIERS = ["Colissimo", "Chronopost", "DHL Express", "Mondial Relay"]

# event_type reçu -> (statut attendu, nouveau statut, événement publié)
TRANSITIONS = {
    "ORDER_CREATED": ("CONFIRMED", "PACKED", "ORDER_PACKED"),
    "ORDER_PACKED": ("PACKED", "SHIPPED", "ORDER_SHIPPED"),
}


def to_json(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(f"Type non sérialisable : {type(obj)}")


def create_if_absent(order):
    # parse_float=Decimal : DynamoDB refuse les float.
    item = json.loads(json.dumps(order), parse_float=Decimal)
    try:
        orders_table.put_item(Item=item, ConditionExpression="attribute_not_exists(order_id)")
        print(json.dumps({"msg": "commande créée depuis l'événement", "order_id": order["order_id"]}))
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def advance(event):
    transition = TRANSITIONS.get(event["event_type"])
    if not transition:
        return
    expected, new_status, next_event = transition
    order_id = event["order"]["order_id"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if SOURCE == "hybrid" and event["event_type"] == "ORDER_CREATED":
        create_if_absent(event["order"])

    update = "SET #s = :new, updated_at = :now, history = list_append(history, :h)"
    values = {":new": new_status, ":expected": expected, ":now": now,
              ":h": [{"status": new_status, "at": now}]}
    if new_status == "SHIPPED":
        update += ", shipping = :ship"
        values[":ship"] = {
            "carrier": random.choice(CARRIERS),
            "tracking_number": "FR" + uuid.uuid4().hex[:12].upper(),
        }

    try:
        order = orders_table.update_item(
            Key={"order_id": order_id},
            UpdateExpression=update,
            ConditionExpression="#s = :expected",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )["Attributes"]
        print(json.dumps({"msg": "statut mis à jour", "order_id": order_id, "status": new_status}))
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Statut déjà à jour : un essai précédent a pu échouer après la mise à jour, en
        # publiant sur SNS. On republie l'événement suivant, sinon la commande reste bloquée.
        order = orders_table.get_item(Key={"order_id": order_id}, ConsistentRead=True).get("Item")
        if not order:
            raise RuntimeError(f"commande {order_id} absente de la table, nouvel essai plus tard")
        if order["status"] != new_status:
            print(json.dumps({"msg": "déjà traité, ignoré", "order_id": order_id, "event": event["event_type"]}))
            return
        print(json.dumps({"msg": "statut déjà à jour, événement republié", "order_id": order_id, "status": new_status}))

    publish(next_event, order)


def publish(event_type, order):
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE,
        "order": order,
    }
    message = {"default": json.dumps(event, default=to_json)}
    if event_type == "ORDER_SHIPPED":
        ship = order["shipping"]
        message["email"] = (
            f"Bonjour {order['customer']['name']},\n\n"
            f"Votre commande {order['order_id']} a été expédiée avec {ship['carrier']}.\n"
            f"Numéro de suivi : {ship['tracking_number']}\n\nÀ bientôt !"
        )
    sns.publish(
        TopicArn=TOPIC_ARN,
        MessageStructure="json",
        Message=json.dumps(message),
        Subject=f"Commande {order['order_id']} : suivi de livraison",
        MessageAttributes={
            "event_type": {"DataType": "String", "StringValue": event_type},
            "source": {"DataType": "String", "StringValue": SOURCE},
        },
    )


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            advance(json.loads(record["body"]))
        except Exception as e:  # noqa: BLE001 - le message sera rejoué puis envoyé en DLQ
            print(json.dumps({"msg": "échec", "error": str(e), "message_id": record["messageId"]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
