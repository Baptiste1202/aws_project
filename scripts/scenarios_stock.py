#!/usr/bin/env python3
"""Scénarios du lot 1 (stock), appelés par test_scenario.sh après les scénarios 1 à 5.

    python3 scripts/scenarios_stock.py <url> <local|aws>

Variables : AUDIT_BUCKET, LOCALSTACK_URL. Les routes d'administration sont appelées
avec un jeton administrateur (Cognito sur AWS, fournisseur local en local : voir
testauth.py). Bibliothèque standard seulement.

Chaque scénario crée son propre produit (identifiant ZZ-nnnnnnnnn unique, voir
testids.py) : les résultats ne dépendent ni du catalogue ni des lancements précédents. Pour l'hybride, une lecture qui suit une écriture attend
la projection (cohérence à terme, 20 s au plus).
Sortie : le détail, puis une ligne « RESULT:✅|❌ nom : détail » par scénario.
"""

import concurrent.futures
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1].rstrip("/")
MODE = sys.argv[2] if len(sys.argv) > 2 else "local"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALSTACK = os.environ.get("LOCALSTACK_URL", "http://localhost:4566")
WAIT = 20


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from testauth import Accounts  # noqa: E402
import testids  # noqa: E402
import testpay  # noqa: E402  (paiement par carte de test, comme Stripe.js)

ACCOUNTS = Accounts(BASE, MODE)
results = []


def info(msg):
    print(f"   {msg}", flush=True)


def call(method, path, body=None, admin=False, url=None, raw=None, headers=None):
    """Renvoie (code HTTP, JSON ou texte)."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url or f"{BASE}/api{path}", data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if admin:
        req.add_header("Authorization", f"Bearer {ACCOUNTS.token('admin')}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode()
            code = r.status
    except urllib.error.HTTPError as e:
        text, code = e.read().decode(), e.code
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def until(fn, timeout=WAIT, step=1):
    """Relit jusqu'à ce que fn() soit vrai (cohérence à terme de l'hybride)."""
    deadline = time.time() + timeout
    while True:
        value = fn()
        if value or time.time() > deadline:
            return value
        time.sleep(step)


def product(pid, admin=True):
    path = "/admin/products" if admin else f"/products/{pid}"
    code, data = call("GET", path, admin=admin)
    if admin:
        return next((p for p in data.get("products", []) if p["id"] == pid), None) if code == 200 else None
    return data if code == 200 else None


def stock(pid):
    p = product(pid)
    return {wh: v["on_hand"] for wh, v in p["stock_by_warehouse"].items()} if p else None


def new_product(par=0, lys=0, threshold=0, price=40):
    """Crée un produit de test, le réapprovisionne et attend qu'il soit lisible. Seuil 0
    par défaut : aucune alerte, donc aucun réappro fournisseur automatique (lot 3b) qui
    changerait le stock attendu ; le scénario 12 fixe son seuil."""
    for _ in range(5):
        pid = testids.product_id("ZZ")
        code, data = call("POST", "/admin/products", {
            "id": pid, "category": "tshirt", "name": f"Produit de test {pid}", "description": "scénario",
            "price": price, "cost_price": price / 2, "color": "Test", "hex": "#888888", "sizes": ["M"],
            "low_stock_threshold": threshold}, admin=True)
        if code == 201:
            break
        if code != 409:
            raise AssertionError(f"création du produit : {code} {data}")
    for wh, qty in (("PAR", par), ("LYS", lys)):
        if qty:
            code, data = call("POST", "/admin/stock/movements", {"product_id": pid, "warehouse": wh,
                                                                 "type": "RESTOCK", "quantity": qty}, admin=True)
            assert code == 201, f"réappro {wh} : {code} {data}"
    assert until(lambda: stock(pid) == {"PAR": par, "LYS": lys}), f"stock de {pid} jamais lisible"
    return pid


def order(pid, qty, cart_id=None, pay=True):
    """Crée la commande puis la paie (carte de test) : elle suit alors tout son parcours."""
    body = {"customer": {"name": "Scénario stock", "email": "test@example.com"},
            "items": [{"product_id": pid, "size": "M", "quantity": qty}]}
    if cart_id:
        body["cart_id"] = cart_id
    code, data = call("POST", "/orders", body)
    if code == 201 and pay:
        paid_code, data = testpay.pay(BASE, data)
        assert paid_code == 200, f"paiement : {paid_code} {data}"
    return code, data


def available(pid):
    p = product(pid, admin=False)
    return p["stock"] if p else None


def scenario(title):
    def wrap(fn):
        def run():
            print(f"\n\033[1m━━ {title}\033[0m", flush=True)
            try:
                detail = fn()
                print(f"   ✅ {detail}")
                results.append(f"✅ {title}")
            except AssertionError as e:
                print(f"   ❌ {e}")
                results.append(f"❌ {title} : {e}")
            except Exception as e:  # noqa: BLE001
                print(f"   ❌ erreur inattendue : {e!r}")
                results.append(f"❌ {title} : erreur inattendue {e!r}")
        return run
    return wrap


# ---------------------------------------------------------------------------

@scenario("6. Back-office : accès, CRUD produit, archivage")
def admin_crud():
    code, _ = call("GET", "/admin/products")
    assert code == 401, f"sans jeton : attendu 401, obtenu {code}"
    code, _ = call("GET", "/admin/products", headers={"Authorization": "Bearer pas-un-jeton"})
    assert code == 401, f"jeton invalide : attendu 401, obtenu {code}"
    info("sans jeton ou avec un jeton invalide : 401")
    pid = new_product(par=3)
    code, data = call("PUT", f"/admin/products/{pid}", {"price": 42.5, "name": "Produit modifié"}, admin=True)
    assert code == 200 and data["price"] == 42.5, f"modification : {code} {data}"
    assert until(lambda: (product(pid) or {}).get("name") == "Produit modifié"), "modification jamais lisible"
    info(f"{pid} créé puis modifié (prix 42,5 €)")
    code, _ = call("DELETE", f"/admin/products/{pid}", admin=True)
    assert code == 200, f"archivage : {code}"
    assert until(lambda: product(pid, admin=False) is None), "produit archivé toujours visible au public"
    code, data = order(pid, 1)
    assert code == 400, f"commande d'un produit archivé : attendu 400, obtenu {code} {data}"
    info("archivé : absent du catalogue public, commande refusée (400)")
    code, _ = call("POST", f"/admin/products/{pid}/archive", {"archived": False}, admin=True)
    assert code == 200 and until(lambda: product(pid, admin=False) is not None), "désarchivage"
    return "CRUD, archivage et désarchivage corrects ; administration protégée"


@scenario("7. Multi-entrepôts : commande servie par Paris puis Lyon")
def multi_warehouse():
    pid = new_product(par=3, lys=2)
    code, data = order(pid, 4)
    assert code == 201, f"commande : {code} {data}"
    alloc = {a["warehouse"]: a["quantity"] for a in data["allocations"]}
    assert alloc == {"PAR": 3, "LYS": 1}, f"répartition inattendue : {alloc}"
    assert until(lambda: stock(pid) == {"PAR": 0, "LYS": 1}), f"stock après commande : {stock(pid)}"
    info(f"4 unités : {alloc} ; stock restant {stock(pid)}")
    return "répartition Paris → Lyon et stock par entrepôt corrects"


@scenario("8. Journal des mouvements : réappro, casse, inventaire, vente")
def movements():
    pid = new_product(par=10)
    for kind, qty in (("BREAKAGE", 2), ("INVENTORY", 7), ("RESTOCK", 5)):
        code, data = call("POST", "/admin/stock/movements",
                          {"product_id": pid, "warehouse": "PAR", "type": kind, "quantity": qty,
                           "reason": "scénario"}, admin=True)
        assert code == 201, f"{kind} : {code} {data}"
    code, data = call("POST", "/admin/stock/movements",
                      {"product_id": pid, "warehouse": "PAR", "type": "BREAKAGE", "quantity": 999}, admin=True)
    assert code == 409, f"casse supérieure au stock : attendu 409, obtenu {code}"
    code, data = order(pid, 3)
    assert code == 201, f"commande : {code} {data}"
    assert until(lambda: stock(pid) == {"PAR": 9, "LYS": 0}), f"stock : {stock(pid)}"

    def journal():
        code, data = call("GET", f"/admin/stock/movements?product_id={pid}", admin=True)
        return [(m["type"], m["delta"], m["on_hand_after"]) for m in reversed(data.get("movements", []))] \
            if code == 200 else []
    expected = [("RESTOCK", 10, 10), ("BREAKAGE", -2, 8), ("INVENTORY", -1, 7), ("RESTOCK", 5, 12), ("SALE", -3, 9)]
    got = until(lambda: journal() if len(journal()) >= 5 else None) or journal()
    assert got == expected, f"journal : {got}"
    info("journal : " + ", ".join(f"{t} {d:+d} → {a}" for t, d, a in got))
    return "5 mouvements dans l'ordre, casse au-delà du stock refusée (409)"


@scenario("9. Annulation : stock remis, impossible une fois expédiée")
def cancel():
    pid = new_product(par=5)
    code, data = order(pid, 2)
    assert code == 201, f"commande : {code}"
    oid = data["order_id"]
    code, data = call("POST", f"/orders/{oid}/cancel")
    assert code == 200 and data["status"] == "CANCELLED", f"annulation : {code} {data}"
    assert until(lambda: stock(pid) == {"PAR": 5, "LYS": 0}), f"stock non remis : {stock(pid)}"
    code, _ = call("POST", f"/orders/{oid}/cancel")
    assert code == 409, f"seconde annulation : attendu 409, obtenu {code}"
    info(f"{oid} annulée, stock remis à 5, seconde annulation refusée (409)")
    code, data = order(pid, 1)
    oid2 = data["order_id"]
    shipped = until(lambda: call("GET", f"/orders/{oid2}")[1].get("status") in ("SHIPPED", "IN_TRANSIT", "DELIVERED"),
                    timeout=90, step=2)
    assert shipped, "commande jamais expédiée"
    code, _ = call("POST", f"/orders/{oid2}/cancel")
    assert code == 409, f"annulation d'une commande expédiée : attendu 409, obtenu {code}"
    time.sleep(8)
    code, data = call("GET", f"/orders/{oid}")
    assert data.get("status") == "CANCELLED", f"la commande annulée a changé de statut : {data.get('status')}"
    info(f"{oid2} expédiée : annulation refusée (409) ; {oid} reste CANCELLED (le traitement l'ignore)")
    return "annulation et remise en stock correctes"


@scenario("10. Réservations : panier, commande, libération, expiration")
def reservations():
    pid = new_product(par=3)
    cart, other = uuid.uuid4().hex, uuid.uuid4().hex
    code, data = call("POST", f"/carts/{cart}/reservations", {"product_id": pid, "quantity": 3})
    assert code == 200 and data["quantity"] == 3, f"réservation : {code} {data}"
    assert until(lambda: available(pid) == 0), f"disponible après réservation : {available(pid)}"
    code, _ = order(pid, 1)
    assert code == 409, f"commande hors panier pendant la réservation : attendu 409, obtenu {code}"
    code, _ = call("POST", f"/carts/{other}/reservations", {"product_id": pid, "quantity": 1})
    assert code == 409, f"réservation concurrente : attendu 409, obtenu {code}"
    info("3/3 réservées : autre commande et autre panier refusés (409)")
    code, data = order(pid, 2, cart_id=cart)
    assert code == 201, f"commande du panier : {code} {data}"
    assert until(lambda: stock(pid) == {"PAR": 1, "LYS": 0} and available(pid) == 1), \
        f"après commande : stock {stock(pid)}, disponible {available(pid)}"
    info("commande de 2 depuis le panier : la 3e unité réservée est libérée")
    code, _ = call("POST", f"/carts/{other}/reservations", {"product_id": pid, "quantity": 1})
    assert code == 200, "réservation de l'unité libérée"
    code, _ = call("DELETE", f"/carts/{other}/reservations/{pid}")
    assert code == 200 and until(lambda: available(pid) == 1), "libération manuelle"
    code, _ = call("POST", f"/carts/{other}/reservations", {"product_id": pid, "quantity": 1, "ttl_seconds": 10})
    assert code == 200 and until(lambda: available(pid) == 0), "réservation de 10 s"
    t0 = time.time()
    assert until(lambda: available(pid) == 1, timeout=150, step=3), "réservation jamais expirée"
    info(f"réservation de 10 s libérée par la purge après {time.time() - t0:.0f} s")
    return "réservation, commande depuis le panier, libération et expiration correctes"


@scenario("11. Réservations simultanées : jamais plus que le stock")
def concurrent_reservations():
    pid = new_product(par=4, lys=2)
    carts = [uuid.uuid4().hex for _ in range(9)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        codes = list(pool.map(lambda c: call("POST", f"/carts/{c}/reservations",
                                             {"product_id": pid, "quantity": 1})[0], carts))
    ok, refused = codes.count(200), codes.count(409)
    limited = sum(1 for c in codes if c in (429, 500))
    assert ok + refused + limited == len(codes), f"réponses inattendues : {codes}"
    assert ok <= 6, f"{ok} réservations pour 6 unités"
    assert until(lambda: available(pid) == 6 - ok), f"disponible {available(pid)}, attendu {6 - ok}"
    p = product(pid)
    reserved = sum(v["reserved"] for v in p["stock_by_warehouse"].values())
    assert reserved == ok, f"réservé {reserved}, attendu {ok}"
    info(f"9 demandes simultanées pour 6 unités : {ok} acceptées, {refused} refusées"
         + (f", {limited} limitées par Lambda" if limited else ""))
    for c in carts:
        call("DELETE", f"/carts/{c}/reservations")
    return f"{ok} réservées ≤ 6, disponible et réservé cohérents"


def audit_events(pid, kind):
    bucket = os.environ.get("AUDIT_BUCKET") or ("threadly-local-audit" if MODE == "local" else subprocess.run(
        ["terraform", f"-chdir={ROOT}/infra", "output", "-raw", "audit_bucket"],
        capture_output=True, text=True).stdout.strip())
    env = dict(os.environ)
    cmd = ["aws"]
    if MODE == "local":
        env.update(AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", AWS_DEFAULT_REGION="eu-west-3")
        cmd += ["--endpoint-url", LOCALSTACK]
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(cmd + ["s3", "cp", f"s3://{bucket}/events/event_type={kind}/", tmp, "--recursive",
                              "--quiet"], env=env, capture_output=True)
        found = []
        for root, _, files in os.walk(tmp):
            for f in files:
                event = json.load(open(os.path.join(root, f)))
                if (event.get("alert") or {}).get("product_id") == pid:
                    found.append(event)
        return found


@scenario("12. Alerte stock bas (SNS → audit S3)")
def low_stock():
    pid = new_product(par=6, threshold=5)
    code, _ = order(pid, 2)
    assert code == 201, f"commande : {code}"
    code, data = call("GET", "/admin/stock/low", admin=True)
    assert until(lambda: any(p["id"] == pid for p in call("GET", "/admin/stock/low", admin=True)[1]["products"])), \
        "produit absent de la liste des stocks bas"
    events = until(lambda: audit_events(pid, "STOCK_LOW"), timeout=60, step=5)
    assert events, "aucun événement STOCK_LOW archivé"
    alert = events[0]["alert"]
    assert alert["available"] == 4 and alert["threshold"] == 5, f"alerte : {alert}"
    info(f"STOCK_LOW publié sur SNS et archivé : {alert['available']} < seuil {alert['threshold']}")
    code, _ = order(pid, 1)
    time.sleep(10)
    assert len(audit_events(pid, "STOCK_LOW")) == 1, "alerte répétée alors que le seuil était déjà franchi"
    info("une seule alerte au franchissement du seuil (pas à chaque vente)")
    return "alerte publiée une fois, liste des stocks bas à jour"


@scenario("13. Import / export CSV du catalogue via S3")
def csv_roundtrip():
    pid = new_product(par=2, price=30)
    code, data = call("POST", "/admin/catalog/export", admin=True)
    assert code == 200 and data.get("url"), f"export : {code} {data}"
    code, text = call("GET", None, url=data["url"])
    assert code == 200 and pid in text, f"téléchargement du CSV : {code}"
    rows = list(csv.DictReader(io.StringIO(text)))
    info(f"export : {len(rows)} produits, lien présigné valable {data['expires_in']} s")
    row = next(r for r in rows if r["id"] == pid)
    row.update(price="33.3", stock_PAR="7")
    new_id = testids.product_id("ZY")
    new_row = {**row, "id": new_id, "name": "Créé par import", "stock_PAR": "4", "stock_LYS": "1"}
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows([row, new_row])
    code, up = call("POST", "/admin/catalog/import-url", admin=True)
    assert code == 200, f"lien d'import : {code} {up}"
    code, _ = call("PUT", None, url=up["url"], raw=buf.getvalue().encode(), headers={"Content-Type": "text/csv"})
    assert code == 200, f"dépôt du CSV : {code}"
    code, data = call("POST", "/admin/catalog/import", {"key": up["key"]}, admin=True)
    assert code == 200 and data["created"] == 1 and data["updated"] == 1, f"import : {code} {data}"
    assert until(lambda: (product(pid) or {}).get("price") == 33.3 and stock(pid) == {"PAR": 7, "LYS": 0}), \
        f"produit mis à jour : {product(pid)}"
    assert until(lambda: stock(new_id) == {"PAR": 4, "LYS": 1}), f"produit créé : {stock(new_id)}"
    info(f"import : {pid} mis à jour (prix 33,3 €, stock Paris 7), {new_id} créé (4 + 1)")
    call("DELETE", f"/admin/products/{new_id}", admin=True)
    return "aller-retour CSV via S3 correct"


@scenario("14. Valorisation du stock")
def valuation():
    pid = new_product(par=4, lys=2, price=40)  # prix de revient 20 €
    code, data = call("GET", "/admin/stock/valuation", admin=True)
    assert code == 200, f"valorisation : {code}"
    lines = [line for line in data["lines"] if line["product_id"] == pid]
    value = sum(line["value"] for line in lines)
    assert abs(value - 120) < 0.01, f"valeur de {pid} : {value}, attendu 6 × 20 = 120"
    total = sum(line["value"] for line in data["lines"])
    assert abs(total - data["total_value"]) < 0.01, f"total {data['total_value']} ≠ somme des lignes {total}"
    by_wh = {wh: v["value"] for wh, v in data["warehouses"].items()}
    info(f"{pid} : 6 × 20 € = {value:.2f} € ; stock total {data['total_value']:.2f} € "
         f"(Paris {by_wh['PAR']:.2f} €, Lyon {by_wh['LYS']:.2f} €)")
    return "valeur par produit, par entrepôt et totale cohérentes"


if __name__ == "__main__":
    for run in (admin_crud, multi_warehouse, movements, cancel, reservations, concurrent_reservations, low_stock,
                csv_roundtrip, valuation):
        run()
    for r in results:
        print(f"RESULT:{r}")
    sys.exit(1 if any(r.startswith("❌") for r in results) else 0)
