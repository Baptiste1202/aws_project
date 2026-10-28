#!/usr/bin/env python3
"""Scénarios du lot 3b (retours et remboursements, réappro fournisseur), appelés par
test_scenario.sh.

    python3 scripts/scenarios_returns.py <url> <local|aws>

Comptes : client, client2, magasinier (voir testauth.py). Paiement : comme
scenarios_order.py (Stripe en mode test sur AWS, faux Stripe en local).
Sortie : le détail, puis une ligne « RESULT:✅|❌ nom : détail » par scénario.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios_client as common  # noqa: E402
import testpay  # noqa: E402
from scenarios_client import BASE, call, info, new_product, order, scenario, until  # noqa: E402
from scenarios_order import HEALTH, MAIL_CUSTOMER, SIMULATED, money, ses_messages, status  # noqa: E402

sys.path.insert(0, os.path.join(common.ROOT, "src"))
from shop import domain  # noqa: E402
from shop.domain import ApiError  # noqa: E402

results = common.results
DELIVERY_WAIT = 120


def movements(pid):
    return call("GET", f"/admin/stock/movements?product_id={pid}", role="magasinier")[1]["movements"]


def stock(pid):
    return (call("GET", f"/products/{pid}")[1] or {}).get("stock")


# ---------------------------------------------------------------------------

@scenario("29. Retour d'une commande livrée : remise en stock et remboursement")
def returned():
    pid = new_product(stock=3, price=30)
    code, o = order(pid, 1, role="client", customer=MAIL_CUSTOMER)
    assert code == 201, f"commande : {code} {o}"
    oid = o["order_id"]
    assert until(lambda: status(oid) == "DELIVERED", timeout=DELIVERY_WAIT, step=2), f"statut : {status(oid)}"
    assert until(lambda: stock(pid) == 2), f"stock après la vente : {stock(pid)}"
    body = {"reason": "size", "comment": "trop petit"}
    assert call("POST", f"/orders/{oid}/return", body)[0] == 401, "retour sans compte : 401 attendu"
    assert call("POST", f"/orders/{oid}/return", body, role="client2")[0] == 403, "retour par un autre client"
    assert call("POST", f"/orders/{oid}/return", {"reason": "drone"}, role="client")[0] == 400, "motif inconnu"
    code, asked = call("POST", f"/orders/{oid}/return", body, role="client")
    assert code == 200 and asked["status"] == "RETURN_REQUESTED", f"demande : {code} {asked}"
    label = asked["return"]["label"]
    assert call("POST", f"/orders/{oid}/return", body, role="client")[0] == 409, "seconde demande acceptée"
    info(f"{oid} livrée : anonyme 401, autre client 403, motif inconnu 400 ; demande acceptée (étiquette {label}), "
         f"seconde demande 409")
    listed = until(lambda: any(r["order_id"] == oid for r in call("GET", "/admin/returns", role="magasinier")[1]
                               ["orders"]))
    assert listed, "retour absent de la liste du magasinier"
    assert call("POST", f"/admin/returns/{oid}/receive", role="client")[0] == 403, "réception par un client"
    code, done = call("POST", f"/admin/returns/{oid}/receive", role="magasinier")
    assert code == 200 and done["status"] == "RETURNED", f"réception : {code} {done}"
    assert done["payment"]["status"] == "refunded", f"remboursement : {done['payment']}"
    again = call("POST", f"/admin/returns/{oid}/receive", role="magasinier")
    assert again[0] == 409, f"seconde réception : {again}"
    refund = "simulé" if SIMULATED else done["payment"]["refund_id"]
    info(f"réceptionné par le magasinier : RETURNED, remboursé {money(done['total'])} € ({refund}) ; "
         f"seconde réception 409")
    assert until(lambda: stock(pid) == 3), f"stock non remis : {stock(pid)}"
    moves = until(lambda: [m for m in movements(pid) if m["type"] == "RETURN" and m["ref"] == oid])
    assert moves and sum(m["delta"] for m in moves) == 1, f"mouvement RETURN : {moves}"
    assert until(lambda: not any(r["order_id"] == oid for r in call("GET", "/admin/returns",
                                                                    role="magasinier")[1]["orders"]))
    records = testpay.stripe_records(BASE, oid)
    if records is not None:
        assert len(records["refunds"]) == 1, f"remboursements chez Stripe : {records['refunds']}"
        info("un seul remboursement chez le faux Stripe (clé d'idempotence threadly-refund-<commande>)")
    info(f"stock remis (3 disponibles), mouvement RETURN +1 réf. {oid}, retiré de la liste des retours")
    def mine():
        return [m for m in ses_messages() if oid in m]
    wanted = ("retour enregistré", "remboursement")
    got = until(lambda: (lambda ms: ms if all(any(w in m for m in ms) for w in wanted) else None)(mine()),
                timeout=60, step=3) or mine()
    missing = [w for w in wanted if not any(w in m for m in got)]
    assert not missing, f"e-mails manquants : {missing}"
    info("e-mails « retour enregistré » (étiquette) et « remboursement » envoyés par SES")
    return "retour demandé par le client, réceptionné une fois : stock remis, remboursé une fois"


@scenario("30. Retour refusé : commande non livrée, délai de 14 jours dépassé")
def refused():
    pid = new_product(stock=2, price=30)
    code, o = order(pid, 1, role="client")
    oid = o["order_id"]
    code, data = call("POST", f"/orders/{oid}/return", {"reason": "size"}, role="client")
    assert code == 409, f"retour d'une commande non livrée : {code} {data}"
    info(f"{oid} ({status(oid)}) : retour refusé, 409 « {data['error']} »")
    user = {"sub": "u1", "groups": []}
    for days, ok in ((13, True), (15, False)):
        at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        livree = {"status": "DELIVERED", "user_id": "u1", "history": [{"status": "DELIVERED", "at": at}]}
        try:
            domain.check_returnable(livree, user)
            accepted = True
        except ApiError as e:
            accepted, reason = False, e.message
        assert accepted == ok, f"livrée il y a {days} jours : {'acceptée' if accepted else 'refusée'}"
        info(f"livrée il y a {days} jours : {'acceptée' if ok else f'refusée ({reason})'} (règle de shop/domain.py)")
    return "seules les commandes livrées depuis moins de 14 jours peuvent être retournées"


@scenario("31. Réappro fournisseur automatique sur stock bas, sans doublon")
def restock():
    expected = "ec2-worker" if HEALTH["backend"] in ("vm", "hybrid") else "step-functions"
    pid = new_product(stock=8, price=20)  # seuil par défaut : 5
    product = call("GET", f"/products/{pid}")[1]
    qty = domain.reorder_quantity(product)

    def move(kind, n):
        code, data = call("POST", "/admin/stock/movements", {"product_id": pid, "warehouse": "PAR", "type": kind,
                                                             "quantity": n}, role="magasinier")
        assert code == 201, f"{kind} {n} : {code} {data}"
    move("BREAKAGE", 4)   # 8 -> 4 : passe sous le seuil, alerte STOCK_LOW
    move("RESTOCK", 5)    # 4 -> 9
    move("BREAKAGE", 5)   # 9 -> 4 : seconde alerte, alors que le bon est encore ouvert
    info(f"{pid} : casse 4, réappro manuel 5, casse 5 -> deux passages sous le seuil (5)")

    def mine():
        return [p for p in call("GET", "/admin/purchase-orders", role="magasinier")[1]["purchase_orders"]
                if p["product_id"] == pid]
    assert until(lambda: mine(), timeout=DELIVERY_WAIT, step=2), "aucun bon de commande créé"
    po = mine()[0]
    info(f"bon {po['po_id']} : {po['quantity']} × {po['name']} chez {po['supplier']}, entrepôt {po['warehouse']}")
    received = until(lambda: all(p["status"] == "RECEIVED" for p in mine()) and mine(), timeout=DELIVERY_WAIT, step=2)
    assert received, f"bon jamais reçu : {mine()}"
    assert len(mine()) == 1, f"bons de commande en double : {[p['po_id'] for p in mine()]}"
    assert until(lambda: stock(pid) == 4 + qty), f"stock après livraison : {stock(pid)} (attendu {4 + qty})"
    moves = [m for m in movements(pid) if m["type"] == "RESTOCK" and m["ref"] == po["po_id"]]
    assert len(moves) == 1 and moves[0]["delta"] == qty, f"mouvement de livraison : {moves}"
    info(f"livré une fois : +{qty} (mouvement RESTOCK réf. {po['po_id']}), stock {stock(pid)} ; "
         f"seconde alerte ignorée (un seul bon ouvert par produit) ; traitement : {expected}")
    return f"bon de commande unique, livré après le délai fournisseur ({expected})"


if __name__ == "__main__":
    for run in (returned, refused, restock):
        run()
    for r in results:
        print(f"RESULT:{r}")
    sys.exit(1 if any(r.startswith("❌") for r in results) else 0)
