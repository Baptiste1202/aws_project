#!/usr/bin/env python3
"""Scénarios du lot 2a (clients), appelés par test_scenario.sh après les scénarios 6 à 14.

    python3 scripts/scenarios_client.py <url> <local|aws>

Comptes et jetons : voir testauth.py (Cognito sur AWS, fournisseur local en local).
Chaque scénario crée ses propres produits (ZX-nnn) et codes promo. Pour l'hybride, une
lecture qui suit une écriture attend la projection (cohérence à terme).
Sortie : le détail, puis une ligne « RESULT:✅|❌ nom : détail » par scénario.
"""

import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/")
MODE = sys.argv[2] if len(sys.argv) > 2 else "local"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "scripts"), os.path.join(ROOT, "local")]
from testauth import Accounts  # noqa: E402
import testids  # noqa: E402  (identifiants uniques à chaque lancement)
import testpay  # noqa: E402  (paiement par carte de test, comme Stripe.js)
import auth_local  # noqa: E402  (clé RSA et signature, pour forger des jetons invalides)

ACCOUNTS = Accounts(BASE, MODE)
WAIT = 20
results = []


def info(msg):
    print(f"   {msg}", flush=True)


def call(method, path, body=None, role=None, token=None, url=None):
    req = urllib.request.Request(url or f"{BASE}/api{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if role:
        token = ACCOUNTS.token(role)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text, code = r.read().decode(), r.status
    except urllib.error.HTTPError as e:
        text, code = e.read().decode(), e.code
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def until(fn, timeout=WAIT, step=1):
    deadline = time.time() + timeout
    while True:
        value = fn()
        if value or time.time() > deadline:
            return value
        time.sleep(step)


def new_product(stock=10, price=40, prefix="ZX"):
    for _ in range(5):
        pid = testids.product_id(prefix)
        code, _ = call("POST", "/admin/products", {
            "id": pid, "category": "pull", "name": f"Produit client {pid}", "description": "scénario client",
            "price": price, "color": "Test", "hex": "#777777", "sizes": ["M"]}, role="admin")
        if code == 201:
            break
    if stock:
        code, data = call("POST", "/admin/stock/movements", {"product_id": pid, "warehouse": "PAR",
                                                             "type": "RESTOCK", "quantity": stock}, role="admin")
        assert code == 201, f"réappro : {code} {data}"
    assert until(lambda: (call("GET", f"/products/{pid}")[1] or {}).get("stock") == stock), "produit jamais lisible"
    return pid


def order(pid, qty=1, role=None, promo=None, pay=True, **extra):
    """Crée la commande puis la paie (carte de test) : elle suit alors tout son parcours."""
    body = {"customer": {"name": "Client test", "email": "test@example.com"},
            "items": [{"product_id": pid, "size": "M", "quantity": qty}], **extra}
    if promo:
        body["promo_code"] = promo
    code, data = call("POST", "/orders", body, role=role)
    if code == 201 and pay:
        paid_code, data = testpay.pay(BASE, data)
        assert paid_code == 200, f"paiement : {paid_code} {data}"
    return code, data


def scenario(title):
    def wrap(fn):
        def run():
            print(f"\n\033[1m━━ {title}\033[0m", flush=True)
            try:
                print(f"   ✅ {fn()}")
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

@scenario("15. Comptes et rôles ; jetons forgés refusés")
def roles():
    cfg = ACCOUNTS.config
    info(f"fournisseur d'identité : {cfg['type']}")
    assert call("GET", "/me")[0] == 401, "/api/me sans jeton : 401 attendu"
    code, me = call("GET", "/me", role="client")
    assert code == 200 and me["groups"] == [], f"profil client : {code} {me}"
    assert call("GET", "/admin/products", role="client")[0] == 403, "client sur l'admin : 403 attendu"
    assert call("GET", "/admin/products", role="magasinier")[0] == 200, "magasinier : lecture du stock"
    pid = new_product(stock=1)
    code, _ = call("POST", "/admin/stock/movements", {"product_id": pid, "warehouse": "LYS", "type": "RESTOCK",
                                                      "quantity": 2}, role="magasinier")
    assert code == 201, f"magasinier : mouvement de stock attendu 201, obtenu {code}"
    assert call("POST", "/admin/promos", {"code": "INTERDIT", "type": "fixed", "value": 5},
                role="magasinier")[0] == 403, "magasinier : codes promo réservés à l'admin (403)"
    assert call("GET", "/admin/promos", role="admin")[0] == 200, "admin : codes promo"
    info("client 403 sur l'admin ; magasinier : stock oui, catalogue et promos non (403) ; admin : tout")

    # Jetons forgés : tous doivent être refusés (401).
    issuer = (f"https://cognito-idp.{cfg['region']}.amazonaws.com/{cfg['user_pool_id']}"
              if cfg["type"] == "cognito" else "http://auth-local:9000")
    real = ACCOUNTS.token("admin")
    header, payload, _ = real.split(".")
    claims = json.loads(auth_local.base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    kid = json.loads(auth_local.base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))["kid"]
    other_key = auth_local.generate_key()
    forged = {
        "signé par une autre clé (même kid, même émetteur)": auth_local.sign_jwt(
            claims, other_key, {"alg": "RS256", "typ": "JWT", "kid": kid}),
        "alg none": f"{auth_local.b64url(json.dumps({'alg': 'none', 'kid': kid}).encode())}.{payload}.",
        "HS256": f"{auth_local.b64url(json.dumps({'alg': 'HS256', 'kid': kid}).encode())}.{payload}."
                 + auth_local.b64url(b"x" * 32),
        "charge modifiée (sub d'un autre)": f"{header}.{auth_local.b64url(json.dumps({**claims, 'sub': 'autre'}).encode())}."
                                           + real.split(".")[2],
        "fournisseur local (émetteur http://auth-local:9000)": auth_local.sign_jwt(
            auth_local.claims_for({"sub": "x", "username": "pirate@example.com", "groups": ["admin"]},
                                  issuer="http://auth-local:9000", client_id="local-client"), other_key),
        "émetteur Cognito d'un autre pool": auth_local.sign_jwt(
            {**claims, "iss": issuer + "x"}, other_key, {"alg": "RS256", "typ": "JWT", "kid": kid}),
    }
    for label, token in forged.items():
        code, data = call("GET", "/admin/products", token=token)
        assert code == 401, f"jeton forgé « {label} » : attendu 401, obtenu {code}"
    info(f"{len(forged)} jetons forgés refusés (401), dont un jeton du fournisseur local")

    # Le fournisseur local n'est référencé nulle part dans l'infrastructure AWS.
    found = []
    for root, _, files in os.walk(os.path.join(ROOT, "infra")):
        if ".terraform" in root:
            continue
        for f in files:
            if f.endswith((".tf", ".tftpl", ".js", ".example")):
                text = open(os.path.join(root, f), encoding="utf-8").read()
                found += [f"{f}:{w}" for w in ("auth-local", "auth_local", "ALLOW_LOCAL_AUTH") if w in text]
    assert not found, f"fournisseur local référencé dans infra/ : {found}"
    info("infra/ ne référence ni auth-local, ni ALLOW_LOCAL_AUTH : non déployable sur AWS")
    return "droits par rôle corrects ; seuls les jetons de l'émetteur configuré sont acceptés"


@scenario("16. Recherche et filtres du catalogue")
def search():
    def ids(query):
        code, data = call("GET", f"/products?{query}")
        assert code == 200, f"{query} : {code} {data}"
        return data["products"]
    baskets = ids("category=baskets")
    assert baskets and all(p["category"] == "baskets" for p in baskets), "filtre catégorie"
    found = ids("q=marini%C3%A8re")
    assert [p["id"] for p in found] == ["TS-003"], f"recherche « marinière » : {[p['id'] for p in found]}"
    cheap = ids("max_price=30&sort=price")
    prices = [p["price"] for p in cheap]
    assert prices and max(prices) <= 30 and prices == sorted(prices), f"prix ≤ 30 triés : {prices}"
    dear = ids("min_price=100&sort=-price")
    assert dear and min(p["price"] for p in dear) >= 100 and [p["price"] for p in dear] == sorted(
        [p["price"] for p in dear], reverse=True), "prix ≥ 100, tri décroissant"
    assert all(p["stock"] > 0 for p in ids("in_stock=1")), "en stock seulement"
    assert call("GET", "/products?sort=n_importe_quoi")[0] == 400, "tri inconnu : 400"
    info(f"catégorie : {len(baskets)} baskets ; « marinière » : TS-003 ; ≤ 30 € : {len(cheap)} produits triés")
    return "recherche texte, catégorie, prix, disponibilité et tris corrects"


@scenario("17. Codes promo : remise, limite d'utilisation, expiration")
def promos():
    run = testids.unique_digits()
    code_ok, code_once, code_old = f"DIX{run}", f"UNE{run}", f"VIEUX{run}"
    pid = new_product(stock=10, price=50)
    assert call("POST", "/admin/promos", {"code": code_ok, "type": "percent", "value": 10, "max_uses": 2},
                role="admin")[0] == 201
    assert call("POST", "/admin/promos", {"code": code_once, "type": "fixed", "value": 5, "max_uses": 1},
                role="admin")[0] == 201
    assert call("POST", "/admin/promos", {"code": code_old, "type": "fixed", "value": 5,
                                          "expires_at": "2020-01-01T00:00:00+00:00"}, role="admin")[0] == 201
    code, data = order(pid, 2, promo=code_ok.lower())
    assert code == 201, f"commande avec {code_ok} : {code} {data}"
    assert (data["subtotal"], data["discount"], data["total"]) == (100, 10, 90), \
        f"remise : sous-total {data['subtotal']}, remise {data['discount']}, total {data['total']}"
    info(f"{code_ok} (10 %) : 100 € → remise 10 € → 90 €")
    assert order(pid, 1, promo=code_ok)[0] == 201, "2e utilisation"
    code, data = order(pid, 1, promo=code_ok)
    assert code == 409 and "épuisé" in data["error"], f"3e utilisation : attendu 409 épuisé, obtenu {code} {data}"
    assert order(pid, 1, promo=code_old)[0] == 409, "code expiré : 409 attendu"
    assert order(pid, 1, promo="INCONNU0")[0] == 409, "code inconnu : 409 attendu"
    info("épuisé après 2 utilisations, expiré et inconnu refusés (409)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        codes = [c for c, _ in pool.map(lambda _: order(pid, 1, promo=code_once), range(4))]
    assert codes.count(201) == 1, f"4 commandes simultanées pour un code à usage unique : {codes}"
    promo = until(lambda: next((p for p in call("GET", "/admin/promos", role="admin")[1]["promos"]
                                if p["code"] == code_once and p["uses"] == 1), None))
    assert promo, "compteur d'utilisations"
    info(f"code à usage unique, 4 commandes simultanées : 1 acceptée, réponses {sorted(codes)}")
    return "remises, limites, expiration et concurrence corrects"


@scenario("18. Avis clients (réservés aux acheteurs)")
def reviews():
    pid = new_product(stock=5)
    assert call("POST", f"/products/{pid}/reviews", {"rating": 5})[0] == 401, "anonyme : 401"
    assert call("POST", f"/products/{pid}/reviews", {"rating": 5}, role="client")[0] == 403, "sans achat : 403"
    code, data = order(pid, 1, role="client")
    assert code == 201 and data.get("user_id"), f"commande connectée : {code} {data}"
    assert call("POST", f"/products/{pid}/reviews", {"rating": 7}, role="client")[0] == 400, "note 7 : 400"
    code, _ = call("POST", f"/products/{pid}/reviews", {"rating": 4, "comment": "Très bien"}, role="client")
    assert code == 201, f"avis : {code}"
    assert until(lambda: (call("GET", f"/products/{pid}")[1].get("rating_count") == 1)), "note moyenne"
    code, _ = call("POST", f"/products/{pid}/reviews", {"rating": 2, "comment": "Finalement bof"}, role="client")
    assert code == 201
    product = until(lambda: (p := call("GET", f"/products/{pid}")[1]).get("rating_avg") == 2 and p)
    assert product and product["rating_count"] == 1, f"un seul avis par client : {product}"
    revs = call("GET", f"/products/{pid}/reviews")[1]["reviews"]
    assert len(revs) == 1 and revs[0]["comment"] == "Finalement bof", f"avis : {revs}"
    info("anonyme 401, sans achat 403, note invalide 400 ; avis remplacé : moyenne 2,0 sur 1 avis")
    return "avis réservés aux acheteurs, moyenne tenue à jour"


@scenario("19. Liste de souhaits et alerte de retour en stock")
def wishlist():
    pid = new_product(stock=0)
    assert call("PUT", f"/me/wishlist/{pid}")[0] == 401, "sans compte : 401"
    assert call("PUT", f"/me/wishlist/{pid}", role="client")[0] == 200
    assert until(lambda: pid in [p["id"] for p in call("GET", "/me/wishlist", role="client")[1]["products"]]), \
        "produit absent de la liste"

    def alerts():
        return [n for n in call("GET", "/me/notifications", role="client")[1]["notifications"]
                if n["product_id"] == pid]
    assert not alerts(), "alerte avant le retour en stock"
    code, _ = call("POST", "/admin/stock/movements", {"product_id": pid, "warehouse": "PAR", "type": "RESTOCK",
                                                      "quantity": 3}, role="admin")
    assert code == 201
    got = until(alerts)
    assert got and len(got) == 1, f"alerte de retour en stock : {got}"
    call("POST", "/admin/stock/movements", {"product_id": pid, "warehouse": "PAR", "type": "RESTOCK",
                                            "quantity": 2}, role="admin")
    time.sleep(3)
    assert len(alerts()) == 1, "nouvelle alerte alors que le produit était déjà en stock"
    info(f"« {got[0]['message']} » : une seule alerte, au retour en stock")
    assert call("DELETE", f"/me/wishlist/{pid}", role="client")[0] == 200
    assert until(lambda: pid not in [p["id"] for p in call("GET", "/me/wishlist", role="client")[1]["products"]])
    return "souhait, alerte unique au retour en stock, retrait"


@scenario("20. Commandes du client connecté")
def my_orders():
    pid = new_product(stock=3)
    code, data = order(pid, 1, role="client2")
    assert code == 201
    mine = until(lambda: data["order_id"] in [o["order_id"] for o in call("GET", "/me/orders", role="client2")[1]["orders"]])
    assert mine, "commande absente de « mes commandes »"
    others = [o["order_id"] for o in call("GET", "/me/orders", role="client")[1]["orders"]]
    assert data["order_id"] not in others, "commande visible par un autre client"
    info(f"{data['order_id']} visible par son client, pas par un autre")
    return "chaque client ne voit que ses commandes"


if __name__ == "__main__":
    for run in (roles, search, promos, reviews, wishlist, my_orders):
        run()
    for r in results:
        print(f"RESULT:{r}")
    sys.exit(1 if any(r.startswith("❌") for r in results) else 0)
