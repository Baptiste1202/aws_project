"""Démarre le workflow Step Functions d'une commande payée, déclenchée par une file SQS :
"processing" (solution serverless) ou "hybrid-processing" (solution hybride, SOURCE=hybrid).

ORDER_PAID -> StartExecution(nom = order_id). Le nom d'une exécution est unique par
machine d'état : un message rejoué ne lance jamais un second workflow
(ExecutionAlreadyExists, ignorée). Les étapes (emballage, expédition, transit,
livraison) sont faites par la Lambda fulfillment, appelée par la machine d'état.
En hybride, la commande vient de RDS : si la projection n'est pas encore passée, elle
est d'abord écrite dans DynamoDB depuis l'événement (écriture conditionnelle).
Serverless seulement (lot 3b) : STOCK_LOW -> StartExecution du workflow de
réapprovisionnement (RESTOCK_STATE_MACHINE_ARN, nom = identifiant de l'événement, donc
une seule exécution par alerte même rejouée). En hybride, le réappro est fait par l'EC2.
La solution VM n'utilise pas Step Functions : son worker EC2 fait les mêmes étapes.
"""

import json
import os

import boto3

from shop.dynamo_store import DynamoStore

SOURCE = os.environ.get("SOURCE", "serverless")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
RESTOCK_STATE_MACHINE_ARN = os.environ.get("RESTOCK_STATE_MACHINE_ARN", "")

store = DynamoStore(os.environ["TABLE"], backend=SOURCE)
sfn = boto3.client("stepfunctions")


def start(machine, name, payload, what):
    try:
        run = sfn.start_execution(stateMachineArn=machine, name=name, input=json.dumps(payload))
        print(json.dumps({"msg": f"workflow {what} démarré", "name": name, "execution": run["executionArn"]}))
    except sfn.exceptions.ExecutionAlreadyExists:
        print(json.dumps({"msg": f"workflow {what} déjà démarré, ignoré", "name": name}))


def process(event):
    if event["event_type"] == "STOCK_LOW" and RESTOCK_STATE_MACHINE_ARN:
        start(RESTOCK_STATE_MACHINE_ARN, event["event_id"], {"alert": event["alert"]}, "réappro")
        return
    if event["event_type"] != "ORDER_PAID":
        return
    order = event["order"]
    if SOURCE == "hybrid" and store.create_order_if_absent(order):
        print(json.dumps({"msg": "commande créée depuis l'événement", "order_id": order["order_id"]}))
    start(STATE_MACHINE_ARN, order["order_id"], {"order_id": order["order_id"]}, "commande")


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            process(json.loads(record["body"]))
        except Exception as e:  # noqa: BLE001 - le message sera rejoué puis envoyé en DLQ
            print(json.dumps({"msg": "échec", "error": str(e), "message_id": record["messageId"]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
