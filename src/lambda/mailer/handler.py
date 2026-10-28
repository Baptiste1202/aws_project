"""E-mails par Amazon SES (toutes solutions), déclenchée par la file SQS "mailer".

Événements : ORDER_PAID (confirmation + facture PDF jointe), ORDER_SHIPPED,
ORDER_DELIVERED, ORDER_CANCELLED, ORDER_RETURN_REQUESTED, ORDER_REFUNDED, BACK_IN_STOCK
(un e-mail par client qui souhaitait le produit), STOCK_LOW, PURCHASE_ORDER_CREATED et
DAILY_REPORT (équipe). Le texte des
messages est dans shop/mail.py. MAIL_REDIRECT : adresse vérifiée qui reçoit tout tant
que le compte SES est en sandbox. MAIL_SKIP_DOMAINS : domaines jamais livrés
(example.com… : commandes de test), pour ne pas consommer le quota SES (200 par jour en
sandbox). Peu de nouveaux essais en cas de refus de SES : la Lambda ne doit pas garder
une exécution du quota du compte pendant des secondes.
"""

import json
import os

import boto3
from botocore.config import Config

from shop import mail

SENDER = os.environ["MAIL_FROM"]
ADMIN = os.environ.get("MAIL_ADMIN", SENDER)
REDIRECT = os.environ.get("MAIL_REDIRECT", "")
SKIP = {d.strip().lower() for d in os.environ.get("MAIL_SKIP_DOMAINS", "").split(",") if d.strip()}
ses = boto3.client("ses", config=Config(retries={"mode": "standard", "max_attempts": 2}))


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            for msg in mail.compose_all(body, SENDER, ADMIN, REDIRECT):
                recipient = msg["X-Threadly-Recipient"] or msg["To"]
                if recipient.rsplit("@", 1)[-1].lower() in SKIP:
                    print(json.dumps({"msg": "e-mail non envoyé (domaine réservé)", "event": body["event_type"],
                                      "subject": msg["Subject"]}, ensure_ascii=False))
                    continue
                sent = ses.send_raw_email(Source=SENDER, Destinations=[msg["To"]],
                                          RawMessage={"Data": msg.as_bytes()})
                print(json.dumps({"msg": "e-mail envoyé", "event": body["event_type"], "subject": msg["Subject"],
                                  "order_id": (body.get("order") or {}).get("order_id"),
                                  "ses_id": sent["MessageId"]}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 - rejoué puis envoyé en DLQ
            print(json.dumps({"msg": "échec", "error": str(e), "message_id": record["messageId"]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
