"""Étape du workflow Step Functions d'une commande (solutions serverless et hybride).

Appelée par la machine d'état avec {"order_id", "event"} : applique la transition de
shop/domain.py (TRANSITIONS) par une mise à jour conditionnelle dans DynamoDB, puis
publie l'événement suivant sur SNS (e-mails, temps réel, audit) :
  ORDER_PAID       -> PACKED      (emballage)
  ORDER_PACKED     -> SHIPPED     (transporteur simulé, numéro de suivi)
  ORDER_SHIPPED    -> IN_TRANSIT  (prise en charge par le transporteur)
  ORDER_IN_TRANSIT -> DELIVERED   (livraison)
Renvoie {"stop": true} si la commande a été annulée entre-temps : la machine s'arrête.
Rejouée (Retry de Step Functions), une étape déjà faite republie seulement l'événement.
La solution VM fait les mêmes transitions dans son worker EC2, sans Step Functions.
"""

import json
import os

import boto3

from shop import domain
from shop.dynamo_store import DynamoStore

SOURCE = os.environ.get("SOURCE", "serverless")
TOPIC_ARN = os.environ["TOPIC_ARN"]
store = DynamoStore(os.environ["TABLE"], backend=SOURCE)
sns = boto3.client("sns")


def handler(event, context):
    order_id, kind = event["order_id"], event["event"]
    result = store.advance({"event_type": kind, "order": {"order_id": order_id}})
    if result is None:
        order = store.get_order(order_id)
        print(json.dumps({"msg": "workflow arrêté", "order_id": order_id, "status": order["status"]}))
        return {"stop": True, "status": order["status"]}
    order, next_event, replayed = result
    print(json.dumps({"msg": "étape rejouée" if replayed else "étape faite", "order_id": order_id,
                      "status": order["status"]}))
    domain.publish(sns, TOPIC_ARN, next_event, order, SOURCE)
    return {"stop": False, "status": order["status"], "next": next_event}
