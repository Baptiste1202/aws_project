#!/usr/bin/env python3
"""Paiement d'une commande de test, comme le ferait Stripe.js dans le navigateur.

Le PaymentIntent est confirmé chez Stripe (ou chez le faux Stripe local) avec la clé
PUBLIABLE et le client_secret renvoyé par POST /api/orders, puis la boutique relit le
paiement (POST /api/orders/{id}/confirm-payment). Aucune clé secrète n'est utilisée.
Cartes de test Stripe : pm_card_visa (acceptée), pm_card_chargeDeclined (refusée).

    python3 scripts/testpay.py <url> <fichier JSON de la réponse de POST /api/orders>
      -> écrit la commande payée (JSON) sur la sortie standard
"""

import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

STRIPE_API = "https://api.stripe.com"
_health = {}


def _request(method, url, data=None, headers=None, form=False):
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def health(base):
    if base not in _health:
        _health[base] = _request("GET", f"{base}/api/health")[1]
    return _health[base]


def confirm_at_stripe(base, order, card="pm_card_visa"):
    """Confirme le PaymentIntent chez Stripe (clé publiable). Renvoie (code, réponse)."""
    conf = health(base)["payment"]
    secret = order["payment"]["client_secret"]
    intent_id = secret.split("_secret_")[0]
    api = conf.get("url") if conf["type"] == "local" else STRIPE_API
    auth = base64.b64encode(f"{conf['publishable_key']}:".encode()).decode()
    return _request("POST", f"{api}/v1/payment_intents/{intent_id}/confirm",
                    {"client_secret": secret, "payment_method": card},
                    {"Authorization": f"Basic {auth}"}, form=True)


def pay(base, order, card="pm_card_visa", confirm=True, headers=None):
    """Paie la commande (si elle attend un paiement) et renvoie (code, commande).
    confirm=False : payée chez Stripe seulement, comme un client qui ferme son
    navigateur avant le retour sur la boutique."""
    if order.get("status") != "PENDING_PAYMENT":
        return 200, order
    code, intent = confirm_at_stripe(base, order, card)
    if code != 200:
        return code, intent
    if not confirm:
        return 200, order
    return _request("POST", f"{base}/api/orders/{order['order_id']}/confirm-payment", {}, headers)


def stripe_records(base, order_id):
    """Faux Stripe local seulement : PaymentIntents et remboursements d'une commande."""
    conf = health(base)["payment"]
    if conf["type"] != "local":
        return None
    return _request("GET", f"{conf['url']}/dev/order/{order_id}")[1]


if __name__ == "__main__":
    url, path = sys.argv[1].rstrip("/"), sys.argv[2]
    status, paid = pay(url, json.load(open(path, encoding="utf-8")))
    json.dump(paid, sys.stdout)
    sys.exit(0 if status == 200 else 1)
