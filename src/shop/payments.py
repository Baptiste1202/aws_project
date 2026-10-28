"""Paiement par carte avec Stripe (mode test), sans dépendance : API REST de Stripe.

Déroulé (pas de webhook, voir le rapport) :
  1. POST /api/orders : la commande est créée en PENDING_PAYMENT (stock réservé pour
     elle), puis un PaymentIntent est créé côté serveur ;
  2. le navigateur confirme le paiement avec Stripe.js (la carte ne passe jamais par
     nos serveurs) ;
  3. POST /api/orders/{id}/confirm-payment : le serveur relit le PaymentIntent chez
     Stripe et ne confirme la commande que s'il a réussi, pour le bon montant ;
  4. une commande restée impayée est relue chez Stripe par la purge : payée (client
     parti avant l'étape 3), elle est confirmée ; sinon le PaymentIntent est annulé
     chez Stripe, puis la commande, et son stock est rendu.

Chaque création et chaque remboursement porte une clé d'idempotence dérivée de
l'order_id : un nouvel essai (délai dépassé, Lambda rejouée) ne crée jamais de doublon.

La clé secrète est lue dans SSM Parameter Store (SecureString) au premier appel.
En local, STRIPE_API_BASE désigne un faux Stripe (local/stripe_local.py), accepté
seulement si ALLOW_LOCAL_STRIPE=1 (défini dans la stack Docker et nulle part ailleurs).
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from shop.domain import ApiError

STRIPE_API = "https://api.stripe.com"
TIMEOUT = 10


class PaymentError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def cents(amount):
    return int((Decimal(str(amount)) * 100).to_integral_value())


def _form(data, prefix=""):
    """Encodage « form » de Stripe : objets imbriqués en clé[sous_clé]."""
    out = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out += _form(v, key)
        else:
            out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out


class StripePayments:
    def __init__(self, secret_loader, publishable_key, api_base=STRIPE_API, public_base=None):
        self._load_secret = secret_loader
        self._secret = None
        self.publishable_key = publishable_key
        self.api_base = api_base.rstrip("/")
        self.public_base = (public_base or api_base).rstrip("/")

    @classmethod
    def from_env(cls, ssm=None):
        """None si Stripe n'est pas configuré (paiement simulé)."""
        param = os.environ.get("STRIPE_SECRET_PARAM")
        publishable = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
        if not param or not publishable:
            return None
        base = os.environ.get("STRIPE_API_BASE", STRIPE_API)
        if base != STRIPE_API and os.environ.get("ALLOW_LOCAL_STRIPE") != "1":
            raise RuntimeError("STRIPE_API_BASE ne peut désigner un autre serveur que Stripe qu'en local")
        if ssm is None:
            import boto3
            ssm = boto3.client("ssm")
        return cls(lambda: ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"],
                   publishable, base, os.environ.get("STRIPE_PUBLIC_BASE"))


    def _call(self, method, path, data=None, idempotency_key=None):
        if self._secret is None:
            self._secret = self._load_secret()
        body = urllib.parse.urlencode(_form(data)).encode() if data else None
        req = urllib.request.Request(f"{self.api_base}{path}", data=body, method=method)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{self._secret}:".encode()).decode())
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                err = json.load(e).get("error", {})
            except ValueError:
                err = {}
            raise PaymentError(e.code, err.get("code") or err.get("type", ""), err.get("message", str(e)))
        except (urllib.error.URLError, TimeoutError) as e:
            raise ApiError(502, f"Stripe injoignable : {e}")

    def create_intent(self, order):
        """PaymentIntent du total de la commande (clé d'idempotence : la commande)."""
        intent = self._call("POST", "/v1/payment_intents", {
            "amount": cents(order["total"]), "currency": "eur",
            "description": f"Threadly {order['order_id']}",
            "metadata": {"order_id": order["order_id"]},
            # Carte seulement, sans redirection : pas de return_url à gérer.
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        }, idempotency_key=f"threadly-pi-{order['order_id']}")
        return intent

    def retrieve(self, intent_id):
        return self._call("GET", f"/v1/payment_intents/{urllib.parse.quote(intent_id)}")

    def cancel(self, intent_id):
        return self._call("POST", f"/v1/payment_intents/{urllib.parse.quote(intent_id)}/cancel",
                          {"cancellation_reason": "abandoned"})

    def refund(self, order, intent_id, amount=None):
        """Remboursement (lot 3b), une seule fois par commande quel que soit le nombre d'essais."""
        data = {"payment_intent": intent_id, "metadata": {"order_id": order["order_id"]}}
        if amount is not None:
            data["amount"] = cents(amount)
        return self._call("POST", "/v1/refunds", data, idempotency_key=f"threadly-refund-{order['order_id']}")


def public_config_from_env():
    """Ce dont le front a besoin pour payer (aucun secret). Lu aussi par la Lambda de
    lecture de l'hybride, qui sert /api/health sans avoir accès à la clé secrète."""
    publishable = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    if not publishable:
        return {"type": "simulated"}
    if os.environ.get("STRIPE_API_BASE", STRIPE_API) == STRIPE_API:
        return {"type": "stripe", "publishable_key": publishable}
    return {"type": "local", "publishable_key": publishable,
            "url": os.environ.get("STRIPE_PUBLIC_BASE") or os.environ["STRIPE_API_BASE"]}


def check_paid(intent, order):
    """Le PaymentIntent prouve-t-il le paiement de CETTE commande, pour son montant ?"""
    return (intent.get("status") == "succeeded"
            and intent.get("amount_received", intent.get("amount")) == cents(order["total"])
            and intent.get("currency") == "eur"
            and (intent.get("metadata") or {}).get("order_id") == order["order_id"])
