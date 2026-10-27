"""Crée dans moto les ressources que Terraform crée sur AWS (topic, files, filtres, mot de passe)."""

import json
import os
import time

import boto3

for attempt in range(30):
    try:
        boto3.client("sts").get_caller_identity()
        break
    except Exception:  # noqa: BLE001 - moto pas encore démarré
        time.sleep(1)

sns = boto3.client("sns")
sqs = boto3.client("sqs")

boto3.client("ssm").put_parameter(Name="/threadly-local/db/password", Value="local-password",
                                  Type="SecureString", Overwrite=True)
topic = sns.create_topic(Name="threadly-order-events")["TopicArn"]

queues = {
    # Même filtre que messaging.tf ; le délai simule l'emballage puis l'expédition.
    "threadly-vm-processing": {
        "delay": os.environ.get("FULFILLMENT_DELAY_SECONDS", "5"),
        "filter": {"source": ["vm"], "event_type": ["ORDER_CREATED", "ORDER_PACKED"]},
    },
}
for name, cfg in queues.items():
    url = sqs.create_queue(QueueName=name, Attributes={"DelaySeconds": cfg["delay"]})["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=arn, Attributes={
        "RawMessageDelivery": "true",
        "FilterPolicy": json.dumps(cfg["filter"]),
    })
    print("file créée :", url)

print("topic créé :", topic)
