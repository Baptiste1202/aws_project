"""Projection CQRS de la solution hybride, déclenchée par la file SQS "hybrid-projection".

RDS (EC2 hybride) fait foi pour le stock, les produits et la création des commandes.
Chaque événement venant de l'EC2 est recopié dans le modèle de lecture DynamoDB, que
lit la Lambda hybrid-read :
  - ORDER_CREATED : la commande (si elle n'y est pas déjà) ;
  - stock : chaque ligne (produit, entrepôt) n'est remplacée que si sa version est plus
    récente : un événement rejoué ou arrivé dans le désordre ne fait jamais revenir le
    stock en arrière ;
  - produits (PRODUCT_CHANGED) : même règle de version ;
  - mouvements : écrits une seule fois (journal) ;
  - bons de commande fournisseur (lot 3b) : même règle de version que le stock.
La logique est dans shop/dynamo_store.py (DynamoStore.project). Le statut des
commandes est géré dans DynamoDB par la Lambda hybrid-processor (et par l'EC2 pour
l'annulation).
"""

import json
import os
import time
from datetime import datetime

from shop.dynamo_store import DynamoStore

store = DynamoStore(os.environ["TABLE"], backend="hybrid")


def delays_ms(record, body):
    """Où passe le temps : publication -> SQS, attente dans SQS, total depuis SQS."""
    attrs = record.get("attributes", {})
    sent = int(attrs.get("SentTimestamp", 0))
    received = int(attrs.get("ApproximateFirstReceiveTimestamp", 0))
    delays = {"sqs_wait_ms": received - sent if sent and received else None,
              "total_since_sqs_ms": int(time.time() * 1000) - sent if sent else None}
    # Publication (EC2) -> arrivée dans SQS, à la seconde près (occurred_at est en secondes).
    if sent and body.get("occurred_at"):
        delays["sns_to_sqs_ms"] = int(sent - datetime.fromisoformat(body["occurred_at"]).timestamp() * 1000)
    return delays


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            for lv in store.project(body):
                print(json.dumps({"msg": "version déjà appliquée ou plus ancienne, ignorée", **lv}))
            print(json.dumps({"msg": "projeté", "event": body["event_type"],
                              "order_id": (body.get("order") or {}).get("order_id"),
                              "stock": len(body.get("stock") or []), **delays_ms(record, body)}))
        except Exception as e:  # noqa: BLE001 - le message sera rejoué puis envoyé en DLQ
            print(json.dumps({"msg": "échec", "error": str(e), "message_id": record["messageId"]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
