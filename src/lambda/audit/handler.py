"""Audit / reporting : archive chaque événement de commande (des deux solutions) dans S3.

Clé : events/event_type=<type>/date=<AAAA-MM-JJ>/<event_id>.json
Ce partitionnement permet de requêter directement l'historique avec Athena.
"""

import json
import os

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["AUDIT_BUCKET"]


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            day = body["occurred_at"][:10]
            key = f"events/event_type={body['event_type']}/date={day}/{body['event_id']}.json"
            s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(body).encode("utf-8"),
                          ContentType="application/json")
            print(json.dumps({"msg": "archivé", "key": key, "source": body.get("source")}))
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"msg": "échec", "error": str(e), "message_id": record["messageId"]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
