"""Libère les réservations de panier expirées et traite les commandes restées impayées
(solution serverless).

Déclenchée chaque minute par une règle EventBridge. Le TTL DynamoDB ne suffit pas :
il supprime l'élément (jusqu'à 48 h plus tard) sans rendre le stock réservé. Chaque
libération est une transaction conditionnelle (rien n'est libéré deux fois, ni une
réservation prolongée entre-temps). Pour la VM et l'hybride, la même purge tourne
sur les EC2, où se font les écritures de stock.

Commande impayée après son délai : le PaymentIntent est relu chez Stripe. Payé (client
parti avant confirm-payment), la commande est confirmée ; sinon il est annulé chez
Stripe, puis la commande, et son stock est rendu (shop/api.py, expire_unpaid).
"""

import json
import os

import boto3

from shop import api, domain
from shop.dynamo_store import DynamoStore
from shop.payments import StripePayments

TOPIC_ARN = os.environ["TOPIC_ARN"]
store = DynamoStore(os.environ["TABLE"], backend="serverless")
sns = boto3.client("sns")


ctx = api.Context(backend="serverless", region=os.environ.get("AWS_REGION", ""),
                  publish=lambda event_type, **payload: domain.publish_event(sns, TOPIC_ARN, event_type, "serverless",
                                                                             **payload),
                  payments=StripePayments.from_env())


def handler(event, context):
    result = store.expire_reservations()
    # Événement, et notifications si un produit souhaité redevient disponible.
    api.publish_changes(store, ctx, "STOCK_CHANGED", result)
    unpaid = api.expire_unpaid(store, ctx)
    print(json.dumps({"msg": "purge", "lignes_de_stock_liberees": len(result["stock"]), **unpaid}))
    return {"released": len(result["stock"]), **unpaid}
