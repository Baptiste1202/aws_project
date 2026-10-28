"""Routeur unique de l'API /api/*, utilisé par la Lambda "api" et par app.py (EC2).

Il ne connaît que l'interface d'un stockage (DynamoStore ou SqlStore). Chaque
écriture renvoie ses changements (commande, niveaux de stock, mouvements, alertes,
produits, codes promo, avis, souhaits), publiés en événements par publish_changes :
audit, alertes, notifications, projection CQRS de l'hybride.

Public
  GET    /api/health                          solution, région, configuration de connexion
  GET    /api/products[?q=&category=&min_price=&max_price=&in_stock=1&sort=price|-price|name|rating]
  GET    /api/products/{id}
  GET    /api/products/{id}/reviews
  POST   /api/orders                          {"customer", "items", "cart_id"?, "promo_code"?,
                                               "shipping_method"?, "payment_ttl"?} -> PENDING_PAYMENT
                                               et payment.client_secret (Stripe.js)
  GET    /api/orders/{id}
  POST   /api/orders/{id}/confirm-payment     relit le PaymentIntent chez Stripe -> CONFIRMED
  POST   /api/orders/{id}/cancel              tant que la commande n'est pas expédiée
  POST   /api/orders/{id}/return              {"reason", "comment"?} : client de la commande, livrée
                                               depuis moins de 14 jours -> RETURN_REQUESTED
  POST   /api/carts/{cart}/reservations       {"product_id", "quantity", "ttl_seconds"?}
  DELETE /api/carts/{cart}/reservations[/{product_id}]
  GET    /api/stats
Client connecté (jeton Cognito, en-tête Authorization: Bearer)
  GET    /api/me                              profil (sub, e-mail, rôles)
  GET    /api/me/orders
  POST   /api/products/{id}/reviews           {"rating" 1-5, "comment"} : produit commandé
  GET    /api/me/wishlist
  PUT    /api/me/wishlist/{id}   DELETE /api/me/wishlist/{id}
  GET    /api/me/notifications                alertes de retour en stock
  POST   /api/orders/{id}/invoice             facture PDF (lien présigné) : client de la commande, admin
Administration
  admin ou magasinier : GET /api/admin/products, GET|POST /api/admin/stock/movements,
                        GET /api/admin/stock/valuation, GET /api/admin/stock/low,
                        GET /api/admin/returns, POST /api/admin/returns/{id}/receive
                        (remise en stock puis remboursement), GET /api/admin/purchase-orders,
                        GET /api/admin/dashboard (chiffre d'affaires, top ventes, ruptures, statuts)
  admin seulement     : POST /api/admin/products, PUT|DELETE /api/admin/products/{id},
                        POST /api/admin/products/{id}/archive, /api/admin/catalog/*,
                        GET|POST /api/admin/promos, PUT /api/admin/promos/{code},
                        POST /api/admin/reports/daily (rapport quotidien à la demande)
Aucune route GET n'écrit (CQRS de l'hybride : les GET sont servis par une Lambda en
lecture seule).
"""

import json
import re
import traceback
from dataclasses import dataclass, field
from typing import Callable

from shop import domain, invoice
from shop.auth import require
from shop.domain import ApiError
from shop.payments import PaymentError, check_paid

STOCK_ROLES = ("admin", "magasinier")
CATALOG_ROLES = ("admin",)


@dataclass
class Context:
    """Ce qui distingue les déploiements : nom de la solution, lecture seule, etc."""
    backend: str                       # "serverless", "vm" ou "hybrid"
    region: str
    read_only: bool = False            # hybride, Lambda de lecture : aucune route d'écriture
    publish: Callable = None           # publish(event_type, **payload) -> événement SNS
    health_extra: dict = field(default_factory=dict)
    stats: Callable = lambda: {"enabled": False}
    auth: object = None                # shop.auth.Authenticator
    files: object = None               # shop.files.S3Files (import / export CSV)
    images: object = None              # shop.files.ImageFiles (images produit)
    images_url: str = ""               # base des miniatures (/images sur AWS)
    realtime_url: str = ""             # API WebSocket du suivi en temps réel (vide : polling)
    payments: object = None            # shop.payments.StripePayments (None : paiement simulé)
    payment_config: dict = field(default_factory=lambda: {"type": "simulated"})  # pour le front
    fulfillment: str = ""              # "step-functions" ou "ec2-worker" (affiché par /api/health)


def publish_changes(store, ctx, event_type, result):
    """Publie l'événement d'une écriture, une alerte par produit passé sous son seuil et,
    pour un produit revenu en stock, les notifications des clients qui le souhaitaient.
    Utilisée aussi par les purges de réservations (Lambda sweeper, EC2)."""
    if not ctx.publish:
        return
    payload = {k: result.get(k) for k in ("stock", "movements", "products", "promos", "reviews", "wishes",
                                          "purchase_orders") if result.get(k)}
    if event_type != "STOCK_CHANGED" or payload:  # un STOCK_CHANGED vide ne sert à rien
        ctx.publish(event_type, order=result.get("order"), **payload)
    for alert in result.get("alerts") or []:
        ctx.publish("STOCK_LOW", alert=alert)
    if result.get("restocked"):
        notifications = store.notify_back_in_stock(result["restocked"])
        ctx.publish("BACK_IN_STOCK", products=None, restocked=result["restocked"],
                    notifications=notifications or None)


# ---------------------------------------------------------------------------
# Paiement (lot 3a) : voir shop/payments.py
# ---------------------------------------------------------------------------

def _paid(store, ctx, order_id, payment):
    """Confirme la commande (une seule fois) et lance son traitement (ORDER_PAID)."""
    order, already = store.mark_paid(order_id, payment)
    if not already:
        publish_changes(store, ctx, "ORDER_PAID", {"order": order})
    return order


def settle_payment(store, ctx, order_id, expire=False):
    """Relit le paiement chez Stripe. Payée : la commande est confirmée. Sinon :
    402 pour le client (confirm-payment) ; pour la purge (expire=True), le
    PaymentIntent est annulé chez Stripe AVANT la commande, ce qui rend tout paiement
    ultérieur impossible (s'il a réussi entre-temps, Stripe refuse l'annulation et la
    commande est confirmée)."""
    order = store.get_order(order_id)
    if order["status"] != "PENDING_PAYMENT":
        if order["status"] in domain.PAID_STATUSES or not expire:
            return order
        return None
    intent_id = (order.get("payment") or {}).get("intent_id")
    if not intent_id:
        if not expire:
            raise ApiError(409, "Aucun paiement en cours pour cette commande")
        intent = None
    else:
        intent = ctx.payments.retrieve(intent_id)
        if check_paid(intent, order):
            return _paid(store, ctx, order_id, {**order["payment"], "status": "succeeded"})
        if intent.get("status") == "succeeded":  # montant ou commande différents : jamais confirmée
            raise ApiError(409, "Paiement incohérent avec la commande")
    if not expire:
        raise ApiError(402, f"Paiement non abouti (statut Stripe : {intent.get('status')})")
    if intent and intent.get("status") != "canceled":
        try:
            ctx.payments.cancel(intent_id)
        except PaymentError:
            intent = ctx.payments.retrieve(intent_id)
            if check_paid(intent, order):  # payée pendant la purge
                return _paid(store, ctx, order_id, {**order["payment"], "status": "succeeded"})
            raise
    result = store.cancel_order(order_id)
    publish_changes(store, ctx, "ORDER_CANCELLED", result)
    return result["order"]


def expire_unpaid(store, ctx):
    """Purge (Lambda sweeper, EC2) : commandes impayées après leur délai. Renvoie
    {"confirmed": [...], "cancelled": [...]}."""
    out = {"confirmed": [], "cancelled": []}
    for order in store.pending_payments(domain.now_iso()):
        try:
            if ctx.payments is None and not (order.get("payment") or {}).get("intent_id"):
                after = settle_payment(store, ctx, order["order_id"], expire=True)
            elif ctx.payments is None:
                continue  # créée avec Stripe, relue par un service qui a la clé
            else:
                after = settle_payment(store, ctx, order["order_id"], expire=True)
        except (ApiError, PaymentError) as e:
            print(json.dumps({"msg": "purge du paiement en échec", "order_id": order["order_id"],
                              "error": getattr(e, "message", str(e))}), flush=True)
            continue
        if after:
            out["confirmed" if after["status"] in domain.PAID_STATUSES else "cancelled"].append(after["order_id"])
    return out


def _create_order(store, ctx, user, body):
    customer, lines, per_product = domain.parse_order(body)
    data = _body(body)
    method, ttl = domain.parse_checkout(data)
    promo = str(data.get("promo_code") or "").strip().upper() or None
    result = store.create_order(customer, lines, per_product, cart_id=data.get("cart_id") or None,
                                promo_code=promo, user=user, shipping_method=method, payment_ttl=ttl)
    order = result["order"]
    if ctx.payments is None:  # pas de clé Stripe : paiement simulé, accepté aussitôt
        publish_changes(store, ctx, "ORDER_CREATED", result)
        return domain.public_order(_paid(store, ctx, order["order_id"], {"provider": "simulated",
                                                                         "status": "succeeded"}))
    try:
        intent = ctx.payments.create_intent(order)
    except (ApiError, PaymentError) as e:
        # Sans paiement possible, la commande est annulée tout de suite (stock rendu).
        publish_changes(store, ctx, "ORDER_CREATED", result)
        publish_changes(store, ctx, "ORDER_CANCELLED", store.cancel_order(order["order_id"]))
        raise ApiError(502, f"Paiement indisponible : {getattr(e, 'message', e)}")
    payment = {"provider": "stripe", "intent_id": intent["id"], "status": "requires_payment"}
    store.set_payment(order["order_id"], payment)
    order["payment"] = payment
    publish_changes(store, ctx, "ORDER_CREATED", result)
    # Seule réponse qui contient le client_secret : le navigateur qui a créé la commande.
    return {**domain.public_order(order), "payment": {**payment, "client_secret": intent["client_secret"]}}


def _cancel(store, ctx, order_id):
    """Annulation par le client. Impayée : le PaymentIntent est annulé chez Stripe
    d'abord (s'il a réussi entre-temps, la commande est confirmée, puis annulée et
    remboursée comme une commande payée). Payée : remboursement après l'annulation."""
    order = store.get_order(order_id)
    payment = order.get("payment") or {}
    if order["status"] == "PENDING_PAYMENT" and payment.get("intent_id") and ctx.payments:
        try:
            ctx.payments.cancel(payment["intent_id"])
        except PaymentError:
            order = settle_payment(store, ctx, order_id)  # 402 s'il n'a pas non plus réussi
            payment = order.get("payment") or {}
    result = store.cancel_order(order_id)
    publish_changes(store, ctx, "ORDER_CANCELLED", result)
    order = result["order"]
    if payment.get("provider") == "stripe" and payment.get("status") == "succeeded" and ctx.payments:
        order = refund(store, ctx, order, payment)
    return order


def refund(store, ctx, order, payment):
    """Remboursement Stripe, jamais en double (clé d'idempotence de la commande).
    Un échec est noté dans la commande (refund_failed) pour être repris. Paiement
    simulé : remboursement simulé, sans appel."""
    if payment.get("provider") != "stripe":
        payment = {**payment, "status": "refunded"}
    else:
        for _ in range(3):
            try:
                r = ctx.payments.refund(order, payment["intent_id"])
                payment = {k: v for k, v in payment.items() if k != "error"}
                payment = {**payment, "status": "refunded", "refund_id": r["id"]}
                break
            except (ApiError, PaymentError) as e:
                error = getattr(e, "message", str(e))
        else:
            payment = {**payment, "status": "refund_failed", "error": error}
            print(json.dumps({"msg": "remboursement en échec", "order_id": order["order_id"], "error": error}),
                  flush=True)
    store.set_payment(order["order_id"], payment)
    order["payment"] = payment
    if payment["status"] == "refunded":
        publish_changes(store, ctx, "ORDER_REFUNDED", {"order": order})
    return order


# ---------------------------------------------------------------------------
# Retours (lot 3b) : demande du client, réception par le magasinier, remboursement
# ---------------------------------------------------------------------------

def _request_return(store, ctx, user, order_id, body):
    order = store.get_order(order_id)
    domain.check_returnable(order, user)
    result = store.request_return(order_id, domain.parse_return(body))
    publish_changes(store, ctx, "ORDER_RETURN_REQUESTED", result)
    return result["order"]


def receive_return(store, ctx, order_id):
    """Le magasinier réceptionne le colis : remise en stock (mouvements RETURN), puis
    remboursement du total. Rejouable : une réception interrompue (stock remis mais
    remboursement en échec) reprend là où elle s'était arrêtée, sans doublon (clé
    d'idempotence Stripe de la commande)."""
    if ctx.payments is None and (store.get_order(order_id).get("payment") or {}).get("provider") == "stripe":
        raise ApiError(503, "Remboursement Stripe impossible depuis ce service")
    result = store.receive_return(order_id)
    already = result.pop("already")
    if already and (result["order"].get("payment") or {}).get("status") in ("refunded", None):
        raise ApiError(409, "Retour déjà réceptionné et remboursé")
    if not already or result["movements"]:
        publish_changes(store, ctx, "ORDER_RETURNED", result)
    order, payment = result["order"], result["order"].get("payment") or {}
    if payment.get("status") in ("succeeded", "refund_failed"):
        order = refund(store, ctx, order, payment)
    return order


# ---------------------------------------------------------------------------
# Réapprovisionnement fournisseur (lot 3b), appelé sur STOCK_LOW par le workflow
# Step Functions (serverless) ou par le worker EC2 (VM, hybride)
# ---------------------------------------------------------------------------

def supplier_order(store, ctx, alert):
    """Crée le bon de commande du produit en alerte. None s'il en a déjà un ouvert."""
    po, created = store.open_purchase_order(alert["product_id"], alert)
    if not created:
        print(json.dumps({"msg": "bon déjà ouvert, alerte ignorée", "po_id": po["po_id"]}), flush=True)
        return None
    publish_changes(store, ctx, "PURCHASE_ORDER_CREATED", {"purchase_orders": [po]})
    return po


def supplier_receive(store, ctx, po_id):
    """Livraison du fournisseur (après le délai simulé) : stock remonté une seule fois."""
    result = store.receive_purchase_order(po_id)
    if not result.pop("already"):
        publish_changes(store, ctx, "PURCHASE_ORDER_RECEIVED", result)
    return result["purchase_orders"][0]


# ---------------------------------------------------------------------------
# Rapport quotidien (lot 4) : EventBridge Scheduler (Lambda reporter, ou worker EC2
# pour la VM), ou à la demande depuis le back-office
# ---------------------------------------------------------------------------

MAX_INLINE_CSV = 100 * 1024  # au-delà, l'e-mail ne porte que le lien (limite SNS : 256 Ko)


def daily_report(store, ctx):
    """CSV des ventes (24 h), du stock et de la valorisation dans S3, puis événement
    DAILY_REPORT : la Lambda mailer l'envoie par SES, CSV joint."""
    products = store.list_products()
    name, text, summary = domain.daily_report(store.list_orders(domain.week_start()), products,
                                              store.valuation(), ctx.backend)
    if not ctx.files:
        raise ApiError(501, "Bucket des rapports non configuré")
    stored = ctx.files.put_report(ctx.backend, name, text)
    report = {**summary, "file": name, "key": stored["key"]}
    if ctx.publish:
        ctx.publish("DAILY_REPORT", report=report, csv=text if len(text) <= MAX_INLINE_CSV else None)
    return {**report, "url": stored["url"]}


def _invoice(store, ctx, user, order_id):
    order = store.get_order(order_id)
    require(user)
    if order.get("user_id") != user["sub"] and "admin" not in user["groups"]:
        raise ApiError(403, "Facture réservée au client de la commande")
    if order["status"] not in domain.PAID_STATUSES:
        raise ApiError(409, f"Pas de facture pour une commande {order['status']}")
    if not ctx.files:
        raise ApiError(501, "Factures non configurées")
    return ctx.files.put_invoice(order_id, invoice.render(order))


def _limit(query):
    try:
        return max(1, min(int((query or {}).get("limit", 100)), 500))
    except ValueError:
        raise ApiError(400, "Paramètre limit invalide")


def _body(raw):
    return domain._json_body(raw)


def _product_id(pid):
    if not domain.PRODUCT_ID_RE.match(pid):
        raise ApiError(400, "Identifiant produit invalide")
    return pid


def handle(store, ctx, method, path, query, body, headers=None):
    """Traite une requête. Renvoie (code HTTP, objet JSON)."""
    path = path.rstrip("/") or "/"
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if method == "HEAD":
        method = "GET"
    try:
        if len(body or "") > domain.MAX_BODY_BYTES:
            raise ApiError(413, "Requête trop volumineuse")
        if method != "GET" and ctx.read_only:
            raise ApiError(405, "Lecture seule : les écritures passent par l'EC2")
        user = ctx.auth.user(headers) if ctx.auth else None  # 401 si le jeton est invalide
        parts = path.split("/")[2:]  # /api/a/b -> ["a", "b"]
        if parts[:1] == ["admin"]:
            return _admin(store, ctx, user, method, parts[1:], query, body)
        if parts[:1] == ["me"]:
            return _me(store, ctx, require(user), method, parts[1:], query)
        return _public(store, ctx, user, method, parts, query, body)
    except ApiError as e:
        return e.status, {"error": e.message}
    except PaymentError as e:  # Stripe a refusé l'appel : jamais de trace interne côté client
        print(json.dumps({"msg": "erreur Stripe", "status": e.status, "code": e.code, "error": e.message}), flush=True)
        return 502, {"error": f"Paiement : {e.message}"}
    except Exception as e:  # noqa: BLE001 - jamais de trace interne côté client
        print(json.dumps({"msg": "erreur interne", "error": str(e), "path": path,
                          "trace": traceback.format_exc(limit=5)}), flush=True)
        return 500, {"error": "Erreur interne"}


def _public(store, ctx, user, method, parts, query, body):
    route = (method, *parts)
    if route == ("GET", "health"):
        return 200, {"status": "ok", "backend": ctx.backend, "region": ctx.region,
                     "auth": ctx.auth.public_config() if ctx.auth else {"type": "none"},
                     "images_url": ctx.images_url, "realtime_url": ctx.realtime_url,
                     "payment": ctx.payment_config, "fulfillment": ctx.fulfillment,
                     "shipping_methods": {k: {"label": v[0], "fee": v[1], "free_from": v[2]}
                                          for k, v in domain.SHIPPING_METHODS.items()}, **ctx.health_extra}
    if route == ("GET", "products"):
        return 200, {"products": domain.search_products(store.list_products(), query)}
    if len(parts) >= 2 and parts[0] == "products":
        pid = _product_id(parts[1])
        if method == "GET" and len(parts) == 2:
            return 200, store.get_product(pid)
        if method == "GET" and parts[2:] == ["reviews"]:
            return 200, {"reviews": store.list_reviews(pid)}
        if method == "POST" and parts[2:] == ["reviews"]:
            rating, comment = domain.validate_review(body)
            result = store.add_review(pid, require(user), rating, comment)
            publish_changes(store, ctx, "REVIEW_ADDED", result)
            return 201, result["review"]
    if route == ("POST", "orders"):
        return 201, _create_order(store, ctx, user, body)
    if method == "GET" and len(parts) == 2 and parts[0] == "orders":
        return 200, domain.public_order(store.get_order(parts[1]))
    if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "confirm-payment":
        if ctx.payments is None:
            return 200, domain.public_order(store.get_order(parts[1]))  # paiement simulé : déjà confirmée
        return 200, domain.public_order(settle_payment(store, ctx, parts[1]))
    if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "invoice":
        return 200, _invoice(store, ctx, user, parts[1])
    if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "cancel":
        return 200, domain.public_order(_cancel(store, ctx, parts[1]))
    if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "return":
        return 200, domain.public_order(_request_return(store, ctx, require(user), parts[1], body))
    if len(parts) >= 3 and parts[0] == "carts" and parts[2] == "reservations":
        cart_id = parts[1]
        if method == "POST" and len(parts) == 3:
            pid, qty, ttl = domain.parse_reservation(cart_id, body)
            result = store.reserve(cart_id, pid, qty, ttl)
            publish_changes(store, ctx, "STOCK_CHANGED", result)
            return 200, result["reservation"]
        if method == "DELETE" and len(parts) in (3, 4):
            domain.check_cart_id(cart_id)
            result = store.release(cart_id, parts[3] if len(parts) == 4 else None)
            publish_changes(store, ctx, "STOCK_CHANGED", result)
            return 200, {"released": len(result["stock"]) > 0}
    if route == ("GET", "stats"):
        return 200, ctx.stats()
    return 404, {"error": "Route inconnue"}


def _me(store, ctx, user, method, parts, query):
    route = (method, *parts)
    if route == ("GET",):
        return 200, {k: user[k] for k in ("sub", "email", "name", "groups")}
    if route == ("GET", "orders"):
        return 200, {"orders": store.my_orders(user["sub"])}
    if route == ("GET", "wishlist"):
        return 200, {"products": store.wishlist(user["sub"])}
    if len(parts) == 2 and parts[0] == "wishlist" and method in ("PUT", "DELETE"):
        result = store.set_wish(user["sub"], _product_id(parts[1]), method == "PUT", user.get("email"))
        publish_changes(store, ctx, "WISHLIST_CHANGED", result)
        return 200, result["wishes"][0]
    if route == ("GET", "notifications"):
        return 200, {"notifications": store.notifications(user["sub"])}
    return 404, {"error": "Route inconnue"}


def _admin(store, ctx, user, method, parts, query, body):
    route = (method, *parts)
    # Consultation et mouvements de stock : magasinier ou admin.
    if route == ("GET", "products"):
        require(user, *STOCK_ROLES)
        return 200, {"products": store.list_products(include_archived=True)}
    if route == ("GET", "stock", "movements"):
        require(user, *STOCK_ROLES)
        pid = (query or {}).get("product_id")
        return 200, {"movements": store.list_movements(_product_id(pid) if pid else None, _limit(query))}
    if route == ("POST", "stock", "movements"):
        require(user, *STOCK_ROLES)
        pid, wh, kind, qty, reason = domain.parse_movement(body)
        result = store.apply_movement(pid, wh, kind, qty, reason)
        publish_changes(store, ctx, "STOCK_CHANGED", result)
        return 201, {"movement": result["movements"][0], "stock": result["stock"]}
    if route == ("GET", "stock", "valuation"):
        require(user, *STOCK_ROLES)
        return 200, store.valuation()
    if route == ("GET", "stock", "low"):
        require(user, *STOCK_ROLES)
        return 200, {"products": store.low_stock()}
    if route == ("GET", "returns"):
        require(user, *STOCK_ROLES)
        return 200, {"orders": [domain.public_order(o) for o in store.list_returns()]}
    if method == "POST" and len(parts) == 3 and parts[0] == "returns" and parts[2] == "receive":
        require(user, *STOCK_ROLES)
        return 200, domain.public_order(receive_return(store, ctx, parts[1]))
    if route == ("GET", "purchase-orders"):
        require(user, *STOCK_ROLES)
        return 200, {"purchase_orders": store.list_purchase_orders(_limit(query))}
    if route == ("GET", "dashboard"):
        require(user, *STOCK_ROLES)
        return 200, domain.dashboard(store.list_orders(domain.week_start()), store.list_products())

    # Catalogue, codes promo, CSV : admin seulement.
    require(user, *CATALOG_ROLES)
    if route == ("POST", "products"):
        pid, fields = domain.validate_product(_body(body))
        result = store.create_product(pid, fields)
        publish_changes(store, ctx, "PRODUCT_CHANGED", result)
        return 201, result["products"][0]
    if len(parts) >= 2 and parts[0] == "products":
        pid = _product_id(parts[1])
        if method == "PUT" and len(parts) == 2:
            _, fields = domain.validate_product(_body(body), partial=True, product_id=pid)
            result = store.update_product(pid, fields)
        elif method == "DELETE" and len(parts) == 2:
            result = store.update_product(pid, {"archived": True})
        elif method == "POST" and parts[2:] == ["archive"]:
            archived = _body(body).get("archived", True)
            result = store.update_product(pid, {"archived": archived is True or str(archived).lower() == "true"})
        elif method == "POST" and parts[2:] == ["image-upload"]:
            # Formulaire présigné pour déposer l'original (aucune écriture en base).
            store.get_product(pid, include_archived=True)
            if not ctx.images:
                raise ApiError(501, "Images non configurées")
            try:
                return 200, ctx.images.upload_form(pid, str(_body(body).get("content_type", "")))
            except ValueError as e:
                raise ApiError(400, str(e))
        elif method == "POST" and parts[2:] == ["image"]:
            image_id = str(_body(body).get("image_id", ""))
            if not re.fullmatch(r"[0-9a-f]{32}", image_id):
                raise ApiError(400, "image_id invalide")
            if not ctx.images or not ctx.images.original_exists(pid, image_id):
                raise ApiError(409, "Image introuvable : déposez-la d'abord avec le formulaire présigné")
            result = store.update_product(pid, {"image_id": image_id})
        else:
            return 404, {"error": "Route inconnue"}
        publish_changes(store, ctx, "PRODUCT_CHANGED", result)
        return 200, result["products"][0]
    if route == ("GET", "promos"):
        return 200, {"promos": store.list_promos()}
    if route == ("POST", "promos") or (method == "PUT" and len(parts) == 2 and parts[0] == "promos"):
        code, fields = domain.validate_promo(_body(body), partial=method == "PUT",
                                             code=parts[1] if method == "PUT" else None)
        result = store.save_promo(code, fields, create=method == "POST")
        publish_changes(store, ctx, "PROMO_CHANGED", result)
        return (201 if method == "POST" else 200), result["promos"][0]
    if route == ("POST", "reports", "daily"):
        return 200, daily_report(store, ctx)
    if route == ("POST", "catalog", "export"):
        return 200, ctx.files.export_csv(domain.catalog_to_csv(store.export_catalog()))
    if route == ("POST", "catalog", "import-url"):
        return 200, ctx.files.import_url()
    if route == ("POST", "catalog", "import"):
        key = str(_body(body).get("key", ""))
        if not re.fullmatch(r"imports/[0-9a-f]{32}\.csv", key):
            raise ApiError(400, "Clé d'import invalide")
        try:
            text = ctx.files.read_import(key)
        except Exception:  # noqa: BLE001
            raise ApiError(404, "Fichier d'import introuvable : déposez-le d'abord avec le lien présigné")
        result = store.import_catalog(domain.csv_to_catalog(text))
        publish_changes(store, ctx, "PRODUCT_CHANGED", result)
        return 200, {"created": result["created"], "updated": result["updated"],
                     "movements": len(result["movements"])}
    return 404, {"error": "Route inconnue"}
