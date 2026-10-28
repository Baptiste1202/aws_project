"""Facture PDF d'une commande, écrite à la main (PDF 1.4, sans dépendance).

Une page A4, police Helvetica (l'une des 14 polices standard : rien à embarquer),
encodage WinAnsi pour les accents et le symbole €. Utilisée par la route
POST /api/orders/{id}/invoice et par la Lambda mailer (pièce jointe de la confirmation).
"""

from decimal import Decimal

from shop.domain import SHIPPING_METHODS

SELLER = ["Threadly SAS (projet FISA 2026, boutique de démonstration)", "Paris, France"]


def _money(value):
    return f"{Decimal(str(value)):.2f}".replace(".", ",") + " €"


def _text(s):
    """Chaîne PDF littérale en WinAnsi (cp1252), parenthèses et antislash échappés."""
    raw = str(s).encode("cp1252", errors="replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _lines(order):
    """(x, y, taille, gras, texte) de chaque ligne de la page."""
    out = [(50, 790, 20, True, "Facture"),
           (50, 765, 10, False, f"Commande {order['order_id']} du {order['created_at'][:10]}")]
    y = 740
    for line in SELLER:
        out.append((50, y, 9, False, line))
        y -= 13
    customer = order["customer"]
    out += [(330, 740, 10, True, "Client"), (330, 727, 10, False, customer["name"]),
            (330, 714, 10, False, customer["email"])]
    if customer.get("address"):
        out.append((330, 701, 10, False, customer["address"][:60]))
    y = 660
    out += [(50, y, 10, True, "Article"), (330, y, 10, True, "Taille"), (390, y, 10, True, "Qté"),
            (430, y, 10, True, "Prix unit."), (500, y, 10, True, "Total")]
    for item in order["items"]:
        y -= 18
        total = Decimal(str(item["unit_price"])) * int(item["quantity"])
        out += [(50, y, 10, False, item["name"][:48]), (330, y, 10, False, item["size"]),
                (390, y, 10, False, str(item["quantity"])), (430, y, 10, False, _money(item["unit_price"])),
                (500, y, 10, False, _money(total))]
    y -= 30
    rows = [("Sous-total", order.get("subtotal", order["total"]))]
    if Decimal(str(order.get("discount") or 0)) > 0:
        rows.append((f"Remise ({order.get('promo_code')})", -Decimal(str(order["discount"]))))
    method = order.get("shipping_method", "standard")
    rows.append((SHIPPING_METHODS.get(method, ("Livraison",))[0], order.get("shipping_fee", 0)))
    for label, value in rows:
        out += [(330, y, 10, False, label), (500, y, 10, False, _money(value))]
        y -= 15
    out += [(330, y - 5, 12, True, "Total TTC"), (500, y - 5, 12, True, _money(order["total"]))]
    payment = order.get("payment") or {}
    paid = "carte bancaire (Stripe, mode test)" if payment.get("provider") == "stripe" else "paiement simulé"
    out.append((50, y - 40, 9, False, f"Payée par {paid}. Document généré automatiquement, sans valeur fiscale."))
    return out


def render(order):
    """Octets du PDF de la facture."""
    content = b"".join(
        b"BT /%s %d Tf %d %d Td %s Tj ET\n" % (b"F2" if bold else b"F1", size, x, y, _text(text))
        for x, y, size, bold, text in _lines(order))
    content += b"0.6 w 50 670 m 545 670 l S\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Title %s /Producer (Threadly) >>" % _text(f"Facture {order['order_id']}"),
    ]
    pdf, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R /Info 7 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(pdf)
