"""Étapes du workflow Step Functions de réapprovisionnement (solution serverless).

Appelée par la machine d'état restock (infra/templates/restock.asl.json) :
  {"action": "order", "alert": {...}} -> bon de commande fournisseur créé (un seul
      ouvert par produit, sinon {"stop": true} : la machine s'arrête) ;
  {"action": "receive", "po_id": ...} -> après le délai fournisseur (état Wait),
      stock remonté par un mouvement RESTOCK, une seule fois même rejouée.
Les règles sont dans shop/api.py (supplier_order, supplier_receive). La VM et
l'hybride font les mêmes étapes dans leur worker EC2, sans Step Functions.
"""

import json
import os

import boto3

from shop import api, domain
from shop.dynamo_store import DynamoStore

TOPIC_ARN = os.environ["TOPIC_ARN"]
store = DynamoStore(os.environ["TABLE"], backend="serverless")
sns = boto3.client("sns")

ctx = api.Context(backend="serverless", region=os.environ.get("AWS_REGION", ""),
                  publish=lambda event_type, **payload: domain.publish_event(sns, TOPIC_ARN, event_type, "serverless",
                                                                             **payload))


def handler(event, context):
    if event["action"] == "order":
        po = api.supplier_order(store, ctx, event["alert"])
        print(json.dumps({"msg": "bon de commande créé" if po else "bon déjà ouvert",
                          "product_id": event["alert"]["product_id"], "po_id": po and po["po_id"]}))
        return {"stop": po is None, "po_id": po and po["po_id"]}
    po = api.supplier_receive(store, ctx, event["po_id"])
    print(json.dumps({"msg": "livraison fournisseur reçue", "po_id": po["po_id"], "quantity": int(po["quantity"])}))
    return {"stop": False, "status": po["status"]}
