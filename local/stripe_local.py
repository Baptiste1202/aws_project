"""Faux Stripe LOCAL (stack Docker seulement), pour tester le paiement sans compte Stripe.

Reproduit le sous-ensemble de l'API Stripe utilisé par shop/payments.py, avec les
comportements dont dépendent les tests :
  - clés d'idempotence (Idempotency-Key) : même clé -> même réponse, aucun doublon ;
  - cartes de test : pm_card_visa acceptée, pm_card_chargeDeclined refusée (402) ;
  - un PaymentIntent payé ne peut plus être annulé (400 payment_intent_unexpected_state) ;
  - un paiement n'est remboursé qu'une fois (400 charge_already_refunded).
Il n'est lancé que par local/docker-compose.yml : Terraform n'y fait aucune référence,
et le code n'accepte une autre adresse que api.stripe.com que si ALLOW_LOCAL_STRIPE=1.

  POST /v1/payment_intents                  (clé secrète)
  GET  /v1/payment_intents/{id}             (clé secrète)
  POST /v1/payment_intents/{id}/confirm     (clé publiable + client_secret, comme Stripe.js)
  POST /v1/payment_intents/{id}/cancel      (clé secrète)
  POST /v1/refunds                          (clé secrète)
  GET  /dev/order/{order_id}                (tests : PaymentIntents et remboursements d'une commande)

Bibliothèque standard seulement.
"""

import base64
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

SECRET = os.environ.get("STRIPE_SECRET_KEY", "sk_test_local")
PUBLISHABLE = os.environ.get("STRIPE_PUBLISHABLE_KEY", "pk_test_local")
PORT = int(os.environ.get("PORT", "12111"))

LOCK = threading.Lock()
INTENTS, REFUNDS, IDEMPOTENCY = {}, {}, {}


class StripeError(Exception):
    def __init__(self, status, code, message, kind="invalid_request_error"):
        super().__init__(message)
        self.status, self.body = status, {"error": {"code": code, "type": kind, "message": message}}


def nested(pairs):
    """metadata[order_id]=x -> {"metadata": {"order_id": "x"}}."""
    out = {}
    for key, value in pairs:
        parts = key.replace("]", "").split("[")
        node = out
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return out


def create_intent(data):
    amount = int(data.get("amount", 0))
    if amount < 50 or data.get("currency") != "eur":
        raise StripeError(400, "parameter_invalid_integer", "Montant ou devise invalide")
    pid = "pi_local_" + secrets.token_hex(8)
    intent = {"id": pid, "object": "payment_intent", "amount": amount, "amount_received": 0,
              "currency": "eur", "status": "requires_payment_method", "metadata": data.get("metadata", {}),
              "description": data.get("description", ""), "client_secret": f"{pid}_secret_{secrets.token_hex(8)}"}
    INTENTS[pid] = intent
    return intent


def confirm(intent, data):
    if intent["status"] == "succeeded":
        raise StripeError(400, "payment_intent_unexpected_state", "Ce PaymentIntent est déjà payé.")
    if intent["status"] == "canceled":
        raise StripeError(400, "payment_intent_unexpected_state", "Ce PaymentIntent est annulé.")
    method = data.get("payment_method", "")
    if method == "pm_card_chargeDeclined":
        intent["status"] = "requires_payment_method"
        raise StripeError(402, "card_declined", "Your card was declined.", "card_error")
    if method != "pm_card_visa":
        raise StripeError(400, "resource_missing", f"Moyen de paiement inconnu : {method}")
    intent.update(status="succeeded", amount_received=intent["amount"], payment_method=method)
    return intent


def cancel(intent):
    if intent["status"] in ("succeeded", "canceled"):
        raise StripeError(400, "payment_intent_unexpected_state",
                          f"Annulation impossible : le PaymentIntent est {intent['status']}.")
    intent["status"] = "canceled"
    return intent


def refund(data):
    intent = INTENTS.get(data.get("payment_intent", ""))
    if not intent or intent["status"] != "succeeded":
        raise StripeError(400, "charge_not_succeeded", "Rien à rembourser pour ce PaymentIntent.")
    if any(r["payment_intent"] == intent["id"] for r in REFUNDS.values()):
        raise StripeError(400, "charge_already_refunded", "Ce paiement a déjà été remboursé.")
    rid = "re_local_" + secrets.token_hex(8)
    REFUNDS[rid] = {"id": rid, "object": "refund", "payment_intent": intent["id"], "status": "succeeded",
                    "amount": int(data.get("amount", intent["amount_received"])), "metadata": data.get("metadata", {})}
    return REFUNDS[rid]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):  # Navigateur : le front local confirme le paiement ici (comme Stripe.js).
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key")
        self.end_headers()

    def _key(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            return base64.b64decode(auth[6:]).decode().split(":")[0]
        return auth[7:] if auth.startswith("Bearer ") else ""

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        data = nested(parse_qsl(self.rfile.read(length).decode())) if length else {}
        key, path = self._key(), self.path.split("?")[0].rstrip("/")
        parts = path.split("/")[1:]
        if parts[:1] == ["dev"] and len(parts) == 3 and parts[1] == "order":
            intents = [i for i in INTENTS.values() if i["metadata"].get("order_id") == parts[2]]
            refunds = [r for r in REFUNDS.values() if r["metadata"].get("order_id") == parts[2]]
            return 200, {"payment_intents": intents, "refunds": refunds}
        if key not in (SECRET, PUBLISHABLE):
            raise StripeError(401, "api_key_invalid", "Clé d'API invalide.")
        idem = self.headers.get("Idempotency-Key")
        if idem and (key, idem) in IDEMPOTENCY:
            return IDEMPOTENCY[(key, idem)]
        result = self._route(method, parts, data, key)
        if idem:
            IDEMPOTENCY[(key, idem)] = result
        return result

    def _route(self, method, parts, data, key):
        if parts[:2] == ["v1", "payment_intents"]:
            if method == "POST" and len(parts) == 2:
                return 200, create_intent(data) if key == SECRET else self._denied()
            intent = INTENTS.get(parts[2]) if len(parts) > 2 else None
            if not intent:
                raise StripeError(404, "resource_missing", "PaymentIntent introuvable.")
            if method == "POST" and parts[3:] == ["confirm"]:
                if key == PUBLISHABLE and data.get("client_secret") != intent["client_secret"]:
                    raise StripeError(400, "payment_intent_invalid_parameter", "client_secret invalide.")
                return 200, confirm(intent, data)
            if key != SECRET:
                return self._denied()
            if method == "GET" and len(parts) == 3:
                return 200, intent
            if method == "POST" and parts[3:] == ["cancel"]:
                return 200, cancel(intent)
        if parts == ["v1", "refunds"] and method == "POST" and key == SECRET:
            return 200, refund(data)
        raise StripeError(404, "resource_missing", "Route inconnue.")

    @staticmethod
    def _denied():
        raise StripeError(401, "secret_key_required", "Cette opération exige la clé secrète.")

    def _dispatch(self, method):
        try:
            with LOCK:
                status, body = self._handle(method)
        except StripeError as e:
            status, body = e.status, e.body
        self._send(status, body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(json.dumps({"msg": "faux Stripe local démarré", "port": PORT}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
