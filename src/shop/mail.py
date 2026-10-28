"""E-mails aux clients et à l'équipe, construits depuis les événements SNS (Lambda mailer).

Un seul endroit pour les trois solutions : la Lambda mailer reçoit les événements par
une file SQS et envoie par Amazon SES. La confirmation de paiement porte la facture PDF
en pièce jointe (un lien présigné par une Lambda expirerait avec ses identifiants
temporaires, bien avant les 7 jours prévus).

SES en « sandbox » (compte sans demande de sortie) n'envoie qu'aux adresses vérifiées :
MAIL_REDIRECT envoie alors tous les e-mails à l'adresse vérifiée, en indiquant le
destinataire prévu dans le message.
"""

from decimal import Decimal
from email.message import EmailMessage

from shop import invoice


def _money(value):
    """Montant d'un événement (float une fois le JSON décodé) : 2 décimales, virgule."""
    return f"{Decimal(str(value)):.2f}".replace(".", ",") + " €"


def _items(order):
    return "\n".join(f"  - {i['quantity']} x {i['name']} (taille {i['size']})" for i in order["items"])


def compose_all(event, sender, admin, redirect=""):
    """Tous les e-mails d'un événement : un par client notifié pour BACK_IN_STOCK (alerte
    de la liste de souhaits), sinon celui de compose (s'il y en a un)."""
    if event["event_type"] == "BACK_IN_STOCK":
        out = []
        for n in event.get("notifications") or []:
            if not n.get("email"):
                continue
            msg = EmailMessage()
            msg["From"], msg["To"] = sender, redirect or n["email"]
            msg["Subject"] = f"De retour en stock : {n.get('name', n['product_id'])}"
            body = (f"Bonjour,\n\n{n['message']} Vous l'aviez ajouté à votre liste de souhaits : il vous "
                    f"attend sur la boutique (référence {n['product_id']}).\n\nÀ bientôt !")
            if redirect and redirect != n["email"]:
                body = f"[SES sandbox : message destiné à {n['email']}]\n\n" + body
            msg["X-Threadly-Event"] = f"BACK_IN_STOCK {event.get('event_id', '')}"
            msg["X-Threadly-Recipient"] = n["email"]
            msg.set_content(body)
            out.append(msg)
        return out
    msg = compose(event, sender, admin, redirect)
    return [msg] if msg is not None else []


def compose(event, sender, admin, redirect=""):
    """EmailMessage de l'événement, ou None si l'événement n'envoie rien."""
    kind, order = event["event_type"], event.get("order")
    attachment = None
    if kind == "STOCK_LOW":
        alert = event["alert"]
        to, subject = admin, f"Stock bas : {alert['product_id']}"
        body = (f"Stock bas : {alert['name']} ({alert['product_id']}) n'a plus que {alert['available']} "
                f"unité(s) disponible(s), sous le seuil de {alert['threshold']}.\n\nSolution : {event['source']}")
    elif kind == "DAILY_REPORT":
        r = event["report"]
        to, subject = admin, f"Rapport quotidien {r['backend']} du {r['generated_at'][:10]}"
        body = (f"Rapport quotidien de la solution {r['backend']} ({r['generated_at']}).\n\n"
                f"Ventes des dernières 24 h : {r['sold_24h']} article(s), {_money(r['revenue_24h'])}\n"
                f"Valeur du stock : {_money(r['stock_value'])} ({r['products']} produits)\n\n"
                f"Fichier : s3://…/{r['key']}" + ("" if event.get("csv") else " (trop volumineux pour être joint)"))
        if event.get("csv"):
            attachment = (r["file"], event["csv"].encode("utf-8"), "text", "csv")
    elif kind == "PURCHASE_ORDER_CREATED":
        # Bon de commande (lot 3b) : au fournisseur en production ; ici à l'équipe (sandbox).
        po = event["purchase_orders"][0]
        to, subject = admin, f"Bon de commande {po['po_id']} : {po['name']}"
        body = (f"Bon de commande fournisseur {po['po_id']}\n\nFournisseur : {po['supplier']}\n"
                f"Article : {po['name']} ({po['product_id']})\nQuantité : {po['quantity']}\n"
                f"Livraison : entrepôt {po['warehouse']}\n\nCréé automatiquement sur alerte de stock bas.\n"
                f"Solution : {event['source']}")
    elif order is None:
        return None
    else:
        name, oid = order["customer"]["name"], order["order_id"]
        to = order["customer"]["email"]
        if kind == "ORDER_PAID":
            subject = f"Commande {oid} : confirmation"
            body = (f"Bonjour {name},\n\nNous avons bien reçu votre paiement : votre commande {oid} est "
                    f"confirmée.\n\n{_items(order)}\n\nLivraison : {_money(order.get('shipping_fee', 0))}\n"
                    f"Total : {_money(order['total'])}\n\nVotre facture est jointe à ce message.\n\nMerci pour "
                    f"votre achat !")
            attachment = (f"facture-{oid}.pdf", invoice.render(order), "application", "pdf")
        elif kind == "ORDER_SHIPPED":
            ship = order["shipping"]
            subject = f"Commande {oid} : expédiée"
            body = (f"Bonjour {name},\n\nVotre commande {oid} a été expédiée avec {ship['carrier']}.\n"
                    f"Numéro de suivi : {ship['tracking_number']}\n\nÀ bientôt !")
        elif kind == "ORDER_DELIVERED":
            subject = f"Commande {oid} : livrée"
            body = (f"Bonjour {name},\n\nVotre commande {oid} a été livrée. Vous pouvez donner votre avis sur "
                    f"les articles depuis votre compte.\n\nÀ bientôt !")
        elif kind == "ORDER_CANCELLED":
            subject = f"Commande {oid} : annulation"
            body = (f"Bonjour {name},\n\nVotre commande {oid} est annulée. Les articles ont été remis en "
                    f"stock.\n\nÀ bientôt !")
        elif kind == "ORDER_RETURN_REQUESTED":
            ret = order.get("return") or {}
            subject = f"Commande {oid} : retour enregistré"
            body = (f"Bonjour {name},\n\nVotre demande de retour pour la commande {oid} est enregistrée.\n"
                    f"Étiquette retour : {ret.get('label', '')} (à coller sur le colis, dépôt en point relais).\n\n"
                    f"Dès réception du colis à notre entrepôt, nous vous rembourserons "
                    f"{_money(order['total'])}, frais de livraison compris.\n\nÀ bientôt !")
        elif kind == "ORDER_REFUNDED":
            subject = f"Commande {oid} : remboursement"
            body = (f"Bonjour {name},\n\nLe remboursement de votre commande {oid} ({_money(order['total'])}) "
                    f"a été effectué sur votre carte.\n\nÀ bientôt !")
        else:
            return None
    msg = EmailMessage()
    msg["From"], msg["Subject"] = sender, subject
    msg["To"] = redirect or to
    if redirect and redirect != to:
        body = f"[SES sandbox : message destiné à {to}]\n\n" + body
    msg["X-Threadly-Event"] = f"{kind} {event.get('event_id', '')}"
    msg["X-Threadly-Recipient"] = to  # destinataire prévu (avant la redirection du bac à sable)
    msg.set_content(body)
    if attachment:
        name, data, maintype, subtype = attachment
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg
