#!/usr/bin/env python3
"""Scénarios du lot 3a (paiement, livraison, facture, e-mails), appelés par test_scenario.sh.

    python3 scripts/scenarios_order.py <url> <local|aws>

Paiement : Stripe en mode test sur AWS (clé publiable seulement, comme Stripe.js),
faux Stripe local dans la stack Docker (voir scripts/testpay.py). Aucune clé secrète
n'est lue par ces tests.
Sortie : le détail, puis une ligne « RESULT:✅|❌ nom : détail » par scénario.
"""

import email
import email.policy
import json
import os
import subprocess
import sys
import time
import urllib.request
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios_client as common  # noqa: E402
import testpay  # noqa: E402
from scenarios_client import ACCOUNTS, BASE, MODE, call, info, new_product, order, scenario, until  # noqa: E402

results = common.results
HEALTH = call("GET", "/health")[1]
LOCALSTACK = os.environ.get("LOCALSTACK_URL", "http://localhost:4566")
PAID = ("CONFIRMED", "PACKED", "SHIPPED", "IN_TRANSIT", "DELIVERED")
# Purge des impayés : toutes les 30 s sur les EC2, chaque minute (EventBridge) en serverless.
PURGE_WAIT = 150
# payment_mode = "simulated" (sans compte Stripe) : commande confirmée dès sa création.
SIMULATED = HEALTH["payment"]["type"] == "simulated"


def status(oid):
    """Statut lu par l'API publique (hybride : modèle de lecture, None tant que la
    commande n'y est pas encore projetée)."""
    data = call("GET", f"/orders/{oid}")[1]
    return data.get("status") if isinstance(data, dict) else None


def money(x):
    return Decimal(str(x)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------

@scenario("23. Paiement par carte (Stripe, mode test)")
def payment():
    conf = HEALTH["payment"]
    if SIMULATED:
        pid = new_product(stock=1, price=30)
        code, o = order(pid, 1, pay=False)
        assert code == 201 and o["status"] == "CONFIRMED", f"paiement simulé : {code} {o}"
        return "sans objet : payment_mode = simulated (commande confirmée aussitôt, vérifié)"
    pid = new_product(stock=3, price=30)
    code, created = order(pid, 1, pay=False)
    assert code == 201 and created["status"] == "PENDING_PAYMENT", f"commande : {code} {created}"
    oid = created["order_id"]
    assert created["payment"].get("client_secret"), "client_secret absent de la réponse du POST"
    public = call("GET", f"/orders/{oid}")[1]
    assert "client_secret" not in json.dumps(public), "client_secret exposé par GET /api/orders/{id}"
    code, data = call("POST", f"/orders/{oid}/confirm-payment")
    assert code == 402, f"confirmation sans paiement : attendu 402, obtenu {code} {data}"
    code, data = testpay.confirm_at_stripe(BASE, created, "pm_card_chargeDeclined")
    assert code == 402 and data["error"]["code"] == "card_declined", f"carte refusée : {code} {data}"
    assert call("POST", f"/orders/{oid}/confirm-payment")[0] == 402, "confirmation après une carte refusée"
    assert until(lambda: status(oid) == "PENDING_PAYMENT"), f"statut après une carte refusée : {status(oid)}"
    info(f"{oid} : sans paiement 402, carte refusée par Stripe (card_declined), toujours PENDING_PAYMENT")
    code, paid = testpay.pay(BASE, created)
    assert code == 200 and paid["status"] in PAID, f"paiement : {code} {paid}"
    again = call("POST", f"/orders/{oid}/confirm-payment")
    assert again[0] == 200 and again[1]["status"] in PAID, f"seconde confirmation : {again}"
    info(f"carte 4242 acceptée : {paid['status']} ; seconde confirmation sans effet ({again[1]['status']})")
    code, data = testpay.confirm_at_stripe(BASE, created)
    assert code == 400, f"second paiement du même PaymentIntent accepté : {code}"
    records = testpay.stripe_records(BASE, oid)
    if records is not None:  # faux Stripe local : un seul PaymentIntent pour la commande
        assert len(records["payment_intents"]) == 1, f"PaymentIntents : {len(records['payment_intents'])}"
        info("un seul PaymentIntent pour la commande (clé d'idempotence threadly-pi-<commande>)")
        # Nouvel essai de création (même clé) : même PaymentIntent, aucun doublon.
        sys.path.insert(0, os.path.join(common.ROOT, "src"))
        from shop.payments import StripePayments
        client = StripePayments(lambda: "sk_test_local", conf["publishable_key"], conf["url"])
        first = client.create_intent({"order_id": "ORD-IDEMPOTENT", "total": Decimal("12.50")})
        second = client.create_intent({"order_id": "ORD-IDEMPOTENT", "total": Decimal("12.50")})
        assert first["id"] == second["id"], "deux PaymentIntents pour la même clé d'idempotence"
        info(f"création rejouée : même PaymentIntent ({first['id']})")
    return "402 sans paiement, carte refusée, paiement unique et vérifié côté serveur"


@scenario("24. Purge des impayés : relecture de Stripe avant d'annuler")
def purge():
    if SIMULATED:
        return "sans objet : payment_mode = simulated (aucune commande impayée)"
    pid = new_product(stock=4, price=25)
    # Client parti après avoir payé chez Stripe, sans revenir sur la boutique.
    code, gone = order(pid, 1, pay=False, payment_ttl=10)
    assert code == 201
    code, _ = testpay.pay(BASE, gone, confirm=False)
    assert code == 200, "paiement chez Stripe"
    # Client parti sans payer.
    code, unpaid = order(pid, 2, pay=False, payment_ttl=10)
    assert code == 201
    info(f"{gone['order_id']} payée chez Stripe sans confirm-payment, {unpaid['order_id']} jamais payée "
         f"(délai de paiement : 10 s)")
    done = until(lambda: status(gone["order_id"]) not in (None, "PENDING_PAYMENT")
                 and status(unpaid["order_id"]) not in (None, "PENDING_PAYMENT"), timeout=PURGE_WAIT, step=5)
    assert done, f"purge jamais passée : {status(gone['order_id'])}, {status(unpaid['order_id'])}"
    assert status(gone["order_id"]) in PAID, f"commande payée annulée par la purge : {status(gone['order_id'])}"
    assert status(unpaid["order_id"]) == "CANCELLED", f"impayée : {status(unpaid['order_id'])}"
    info(f"purge : {gone['order_id']} confirmée ({status(gone['order_id'])}), {unpaid['order_id']} annulée")
    assert until(lambda: call("GET", f"/products/{pid}")[1].get("stock") == 3), \
        f"stock de l'impayée non rendu : {call('GET', f'/products/{pid}')[1].get('stock')}"
    code, data = testpay.confirm_at_stripe(BASE, unpaid)
    assert code == 400, f"paiement encore possible après l'annulation : {code} {data}"
    info("stock de l'impayée rendu (3 disponibles) ; son PaymentIntent est annulé chez Stripe (paiement refusé)")
    return "payée mais non confirmée : confirmée ; impayée : PaymentIntent puis commande annulés"


@scenario("25. Frais de port")
def shipping():
    pid = new_product(stock=6, price=40)
    cases = [(1, "standard", None, "4.90"), (2, "standard", None, "0.00"), (1, "express", None, "9.90"),
             (2, "standard", "QUARANTE", "4.90")]
    code, _ = call("POST", "/admin/promos", {"code": "QUARANTE", "type": "fixed", "value": 40}, role="admin")
    assert code in (201, 409), f"code promo : {code}"
    until(lambda: any(p["code"] == "QUARANTE" for p in call("GET", "/admin/promos", role="admin")[1]["promos"]))
    lines = []
    for qty, method, promo, fee in cases:
        code, o = order(pid, qty, pay=False, promo=promo, shipping_method=method)
        assert code == 201, f"commande {qty} x 40 € {method} : {code} {o}"
        expected = money(40 * qty) - money(o["discount"]) + money(fee)
        assert money(o["shipping_fee"]) == money(fee) and money(o["total"]) == expected, \
            f"{qty} x 40 € {method} {promo or ''} : frais {o['shipping_fee']}, total {o['total']}"
        lines.append(f"{qty} x 40 € {method}{' − 40 € (promo)' if promo else ''} : frais {fee} €, total {o['total']} €")
        assert call("POST", f"/orders/{o['order_id']}/cancel")[0] == 200  # stock rendu aussitôt
    for line in lines:
        info(line)
    code, data = order(pid, 1, pay=False, shipping_method="drone")
    assert code == 400, f"mode inconnu : {code}"
    return "standard 4,90 € (offerte dès 60 € après remise), express 9,90 €, mode inconnu refusé"


@scenario("26. Workflow de livraison jusqu'à DELIVERED ; annulation payée remboursée")
def workflow():
    expected = "ec2-worker" if HEALTH["backend"] == "vm" else "step-functions"
    assert HEALTH["fulfillment"] == expected, f"traitement : {HEALTH['fulfillment']} (attendu {expected})"
    pid = new_product(stock=4, price=30)
    code, o = order(pid, 1, shipping_method="express")
    assert code == 201
    oid = o["order_id"]
    delivered = until(lambda: status(oid) == "DELIVERED", timeout=120, step=2)
    data = call("GET", f"/orders/{oid}")[1]
    assert delivered, f"statut : {data.get('status')}"
    history = [h["status"] for h in data["history"]]
    assert history == ["PENDING_PAYMENT", "CONFIRMED", "PACKED", "SHIPPED", "IN_TRANSIT", "DELIVERED"], history
    assert data["shipping"]["carrier"] in ("Chronopost", "DHL Express"), f"transporteur express : {data['shipping']}"
    info(f"{oid} ({expected}) : {' → '.join(history)} ; {data['shipping']['carrier']} "
         f"{data['shipping']['tracking_number']}")
    # Payée puis annulée tout de suite : remboursée une fois, et le workflow s'arrête.
    code, o2 = order(pid, 1)
    oid2 = o2["order_id"]
    code, cancelled = call("POST", f"/orders/{oid2}/cancel")
    assert code == 200 and cancelled["status"] == "CANCELLED", f"annulation : {code} {cancelled}"
    if not SIMULATED:
        assert cancelled["payment"]["status"] == "refunded", f"remboursement : {cancelled['payment']}"
    time.sleep(15)
    assert status(oid2) == "CANCELLED", f"le workflow a continué : {status(oid2)}"
    records = testpay.stripe_records(BASE, oid2)
    if records is not None:
        assert len(records["refunds"]) == 1, f"remboursements : {records['refunds']}"
    refund = "rien à rembourser (paiement simulé)" if SIMULATED else f"remboursée ({cancelled['payment']['refund_id']})"
    info(f"{oid2} payée puis annulée : {refund}, reste CANCELLED")
    return f"5 étapes par {expected}, annulation d'une commande payée remboursée une fois"


@scenario("27. Facture PDF")
def invoices():
    pid = new_product(stock=3, price=35)
    code, anon = order(pid, 1)
    assert call("POST", f"/orders/{anon['order_id']}/invoice")[0] == 401, "facture sans compte : 401"
    code, mine = order(pid, 1, role="client", pay=False)
    oid = mine["order_id"]
    if not SIMULATED:
        assert call("POST", f"/orders/{oid}/invoice", role="client")[0] == 409, "facture d'une commande impayée"
    testpay.pay(BASE, mine)
    until(lambda: status(oid) in PAID)
    assert call("POST", f"/orders/{oid}/invoice", role="client2")[0] == 403, "facture d'un autre client : 403"
    code, link = call("POST", f"/orders/{oid}/invoice", role="client")
    assert code == 200 and link["url"], f"facture : {code} {link}"
    with urllib.request.urlopen(link["url"], timeout=30) as r:
        pdf, ctype = r.read(), r.headers.get("Content-Type")
    assert pdf.startswith(b"%PDF-1.4") and oid.encode() in pdf and pdf.rstrip().endswith(b"%%EOF"), "PDF invalide"
    assert call("POST", f"/orders/{oid}/invoice", role="admin")[0] == 200, "facture pour l'admin"
    info(f"{oid} : PDF {len(pdf)} octets ({ctype}), lien présigné {link['expires_in']} s ; "
         f"anonyme 401, impayée 409, autre client 403, admin 200")
    return "facture réservée au client de la commande (et à l'admin), une fois payée"


# Les commandes de test utilisent test@example.com, que le mailer n'envoie pas (domaine
# réservé, quota SES du bac à sable) : les scénarios qui vérifient les e-mails des
# clients commandent avec une adresse en threadly.test (RFC 6761).
MAIL_CUSTOMER = {"name": "Client e-mails", "email": "client-emails@threadly.test"}


def ses_messages():
    """Messages envoyés : LocalStack (/_aws/ses) en local, journaux de la Lambda mailer sur AWS."""
    if MODE == "local":
        with urllib.request.urlopen(f"{LOCALSTACK}/_aws/ses", timeout=10) as r:
            raw = [m.get("RawData") or "" for m in json.load(r).get("messages", [])]
        out = []
        for data in raw:  # objet décodé (RFC 2047), noms des pièces jointes
            msg = email.message_from_string(data, policy=email.policy.default)
            files = [part.get_filename() for part in msg.iter_attachments()]
            out.append(f"{msg['Subject']} | pièces jointes : {files}")
        return out
    out = subprocess.run(["aws", "logs", "filter-log-events", "--log-group-name", "/aws/lambda/threadly-dev-mailer",
                          "--start-time", str(int((time.time() - 1800) * 1000)), "--filter-pattern", '"e-mail envoyé"',
                          "--query", "events[].message", "--output", "json"], capture_output=True, text=True)
    return json.loads(out.stdout or "[]")


@scenario("28. E-mails (SES) : confirmation avec facture, expédition, livraison, retour en stock")
def emails():
    pid = new_product(stock=2, price=45)
    code, o = order(pid, 1, customer=MAIL_CUSTOMER)
    oid = o["order_id"]
    assert until(lambda: status(oid) == "DELIVERED", timeout=120, step=2), f"statut : {status(oid)}"

    def mine():
        return [m for m in ses_messages() if oid in m]
    wanted = ("confirmation", "expédiée", "livrée")
    got = until(lambda: (lambda ms: ms if all(any(w in m for m in ms) for w in wanted) else None)(mine()),
                timeout=60, step=3) or mine()
    missing = [w for w in wanted if not any(w in m for m in got)]
    assert not missing, f"e-mails manquants pour {oid} : {missing} (reçus : {len(got)})"
    if MODE == "local":
        confirmation = next(m for m in got if "confirmation" in m)
        assert f"facture-{oid}.pdf" in confirmation, f"facture absente de la confirmation : {confirmation}"
        info(f"{len(got)} e-mails dans SES (LocalStack) pour {oid}, facture jointe à la confirmation")
    else:
        info(f"{len(got)} e-mails envoyés par SES pour {oid} (journaux de la Lambda mailer)")
    # Alerte de retour en stock de la liste de souhaits : un e-mail au client.
    wished = new_product(stock=0, price=45)
    code, _ = call("PUT", f"/me/wishlist/{wished}", role="client")
    assert code == 200, f"liste de souhaits : {code}"
    time.sleep(3)  # hybride : le souhait est projeté avant le réappro
    code, _ = call("POST", "/admin/stock/movements", {"product_id": wished, "warehouse": "PAR", "type": "RESTOCK",
                                                     "quantity": 2}, role="admin")
    assert code == 201, f"réappro : {code}"
    back = until(lambda: [m for m in ses_messages() if "De retour en stock" in m and wished in m], timeout=90, step=3)
    assert back, f"e-mail de retour en stock absent pour {wished}"
    info(f"{wished} souhaité par le client puis réapprovisionné : e-mail « De retour en stock » envoyé par SES")
    return "confirmation (facture jointe), expédition, livraison et retour en stock envoyés par SES"


if __name__ == "__main__":
    for run in (payment, purge, shipping, workflow, invoices, emails):
        run()
    for r in results:
        print(f"RESULT:{r}")
    sys.exit(1 if any(r.startswith("❌") for r in results) else 0)
