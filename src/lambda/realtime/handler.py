"""Connexions WebSocket (API Gateway) du suivi en temps réel.

Une seule API pour les trois solutions : le front indique la sienne à la connexion.
Routes : $connect (?shop=serverless|vm|hybrid, jeton facultatif &token=<JWT Cognito>),
subscribe ({"action": "subscribe", "order_id": "ORD-..."}), $disconnect.

Table "realtime" (TTL de 2 h sur chaque élément) :
  CONN#<connexion>          META             solution, utilisateur éventuel, commandes suivies
  ORDER#<solution>#<cmd>    CONN#<connexion> abonnement au suivi d'une commande
  USER#<solution>#<sub>     CONN#<connexion> alertes du client connecté
La Lambda notifier lit ces éléments pour pousser les événements (statut de commande,
retour en stock) aux bonnes connexions.
"""

import json
import os
import re
import time

import boto3

from shop.auth import Authenticator
from shop.domain import ApiError

TTL = 2 * 3600
ORDER_RE = re.compile(r"^ORD-[0-9A-F]{10}$")
SHOPS = ("serverless", "vm", "hybrid")
MAX_ORDERS = 20
table = boto3.resource("dynamodb").Table(os.environ["REALTIME_TABLE"])
auth = Authenticator.from_env()


def reply(status, body=None):
    return {"statusCode": status, "body": json.dumps(body or {})}


def handler(event, context):
    ctx = event["requestContext"]
    conn, route = ctx["connectionId"], ctx["routeKey"]
    expires = int(time.time()) + TTL
    if route == "$connect":
        params = event.get("queryStringParameters") or {}
        shop, token = params.get("shop"), params.get("token")
        if shop not in SHOPS:
            return reply(400, {"error": "paramètre shop invalide"})
        user = None
        if token:
            try:
                user = auth.verify(token)["sub"]
            except ApiError:
                return reply(401, {"error": "jeton invalide"})  # connexion refusée
        table.put_item(Item={"pk": f"CONN#{conn}", "sk": "META", "shop": shop, "user": user or "", "orders": [],
                             "ttl": expires})
        if user:
            table.put_item(Item={"pk": f"USER#{shop}#{user}", "sk": f"CONN#{conn}", "ttl": expires})
        return reply(200)
    if route == "subscribe":
        try:
            order_id = str(json.loads(event.get("body") or "{}").get("order_id", ""))
        except json.JSONDecodeError:
            order_id = ""
        if not ORDER_RE.match(order_id):
            return reply(400, {"error": "order_id invalide"})
        meta = table.get_item(Key={"pk": f"CONN#{conn}", "sk": "META"}).get("Item")
        if not meta:
            return reply(410, {"error": "connexion inconnue"})
        if order_id in meta.get("orders", []):
            return reply(200, {"subscribed": order_id})
        if len(meta.get("orders", [])) >= MAX_ORDERS:
            return reply(429, {"error": f"{MAX_ORDERS} commandes suivies au plus par connexion"})
        table.put_item(Item={"pk": f"ORDER#{meta['shop']}#{order_id}", "sk": f"CONN#{conn}", "ttl": expires})
        table.update_item(Key={"pk": f"CONN#{conn}", "sk": "META"},
                          UpdateExpression="SET orders = list_append(if_not_exists(orders, :e), :o)",
                          ExpressionAttributeValues={":o": [order_id], ":e": []})
        return reply(200, {"subscribed": order_id})
    if route == "$disconnect":
        meta = table.get_item(Key={"pk": f"CONN#{conn}", "sk": "META"}).get("Item") or {}
        for order_id in meta.get("orders", []):
            table.delete_item(Key={"pk": f"ORDER#{meta['shop']}#{order_id}", "sk": f"CONN#{conn}"})
        if meta.get("user"):
            table.delete_item(Key={"pk": f"USER#{meta['shop']}#{meta['user']}", "sk": f"CONN#{conn}"})
        table.delete_item(Key={"pk": f"CONN#{conn}", "sk": "META"})
        return reply(200)
    return reply(400, {"error": "route inconnue"})

