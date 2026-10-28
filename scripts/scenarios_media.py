#!/usr/bin/env python3
"""Scénarios du lot 2b (images, temps réel), appelés par test_scenario.sh après le 20.

    python3 scripts/scenarios_media.py <url> <local|aws>

21 : image produit déposée par formulaire présigné, miniatures WebP par la Lambda
     thumbnailer (local et AWS).
22 : suivi en temps réel par API Gateway WebSocket (AWS seulement : pas de WebSocket
     dans LocalStack gratuit, le front y suit les commandes par polling). Client
     WebSocket écrit ici avec la bibliothèque standard.
Sortie : le détail, puis une ligne « RESULT:✅|❌ nom : détail » par scénario.
"""

import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios_client as common  # noqa: E402  (comptes, appels, produits de test)
from scenarios_client import ACCOUNTS, BASE, MODE, call, info, new_product, order, scenario, until  # noqa: E402
import testpay  # noqa: E402

results = common.results
HEALTH = call("GET", "/health")[1]


def png(width, height):
    """PNG en dégradé, encodé à la main (zlib) : aucune dépendance."""
    rows = b"".join(b"\x00" + b"".join(bytes((x * 255 // width, y * 255 // height, 128)) for x in range(width))
                    for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def post_form(form, content, content_type=None):
    """Envoie un formulaire POST présigné S3 (multipart, le fichier en dernier)."""
    boundary = uuid.uuid4().hex
    fields = dict(form["fields"])
    if content_type:
        fields["Content-Type"] = content_type
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="image"\r\n'
                 f'Content-Type: {fields["Content-Type"]}\r\n\r\n'.encode() + content + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(form["url"], data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, b"", ""


def images_base():
    url = HEALTH.get("images_url", "")
    return url if url.startswith("http") else f"{BASE}{url}"


def webp_size(data):
    """Largeur et hauteur d'un WebP (en-têtes VP8, VP8L ou VP8X)."""
    kind = data[12:16]
    if kind == b"VP8 ":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if kind == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    raise AssertionError(f"pas un WebP : {data[:16]!r}")


# ---------------------------------------------------------------------------

@scenario("21. Image produit : dépôt présigné et miniatures WebP")
def images():
    pid = new_product(stock=2, prefix="ZM")
    path = f"/admin/products/{pid}/image-upload"
    assert call("POST", path, {"content_type": "image/png"})[0] == 401, "anonyme : 401"
    assert call("POST", path, {"content_type": "image/png"}, role="magasinier")[0] == 403, "magasinier : 403"
    code, data = call("POST", path, {"content_type": "text/html"}, role="admin")
    assert code == 400, f"type text/html : {code} {data}"
    code, data = call("POST", f"/admin/products/{pid}/image", {"image_id": uuid.uuid4().hex}, role="admin")
    assert code == 409, f"image jamais déposée : {code} {data}"

    code, form = call("POST", path, {"content_type": "image/png"}, role="admin")
    assert code == 200 and form["key"].startswith(f"originals/{pid}/"), f"formulaire : {code} {form}"
    if MODE == "aws":
        # La politique signée impose le type : un autre type est refusé par S3.
        refused = post_form(form, b"<html>pas une image</html>", content_type="text/html")
        assert refused == 403, f"type modifié accepté par S3 : {refused}"
    image = png(900, 600)
    status = post_form(form, image)
    assert status in (200, 201, 204), f"dépôt de l'original : {status}"
    code, product = call("POST", f"/admin/products/{pid}/image", {"image_id": form["image_id"]}, role="admin")
    assert code == 200 and product["image_id"] == form["image_id"], f"rattachement : {code} {product}"
    info(f"original PNG {len(image) // 1024} Ko déposé dans S3 ({form['key']}), rattaché à {pid}")
    assert until(lambda: call("GET", f"/products/{pid}")[1].get("image_id") == form["image_id"]), \
        "image_id absent du catalogue public"

    sizes = {}
    for size in (160, 480):
        url = f"{images_base()}/{pid}/{form['image_id']}-{size}.webp"
        got = until(lambda: (lambda r: r if r[0] == 200 else None)(fetch(url)), timeout=60, step=2)
        assert got, f"miniature {size} jamais servie : {url}"
        sizes[size] = webp_size(got[1])
        assert max(sizes[size]) == size, f"miniature {size} : {sizes[size]}"
    info(f"miniatures servies : 160 -> {sizes[160][0]}x{sizes[160][1]}, 480 -> {sizes[480][0]}x{sizes[480][1]} "
         f"(sous {images_base()})")

    # Un fichier qui n'est pas une image, déposé avec un type image/png, est ignoré.
    code, bad = call("POST", path, {"content_type": "image/png"}, role="admin")
    assert post_form(bad, b"ceci n'est pas une image") in (200, 201, 204)
    time.sleep(8)
    assert fetch(f"{images_base()}/{pid}/{bad['image_id']}-160.webp")[0] != 200, "miniature d'un faux PNG"
    info("faux PNG ignoré par la Lambda (aucune miniature)")
    if MODE == "aws":
        # La VM renvoie index.html (200) pour un chemin inconnu : on compare le contenu.
        code, body, ctype = fetch(f"{BASE}/{form['key']}")
        assert body != image and not ctype.startswith("image/"), f"original public par CloudFront : {code} {ctype}"
        info(f"original non public (GET /{form['key']} : {code} {ctype or 'sans contenu'})")
    return "formulaire présigné réservé à l'admin, miniatures 160 et 480 px, faux fichier ignoré"


# ---------------------------------------------------------------- WebSocket

class WebSocket:
    """Client WebSocket minimal (RFC 6455) : texte, ping/pong, fermeture."""

    def __init__(self, url, timeout=10):
        u = urllib.parse.urlparse(url)
        raw = socket.create_connection((u.hostname, u.port or 443), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        target = u.path + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {target} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            head += chunk
        self.status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
        self.buffer = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""

    def _read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connexion fermée")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _send(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes((0x80 | opcode,)) + (bytes((0x80 | n,)) if n < 126 else bytes((0x80 | 126,)) + struct.pack(">H", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, obj):
        self._send(1, json.dumps(obj).encode())

    def recv(self, timeout):
        """Prochain message texte (objet JSON), ou None à l'expiration du délai."""
        self.sock.settimeout(timeout)
        try:
            while True:
                b1, b2 = self._read(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                payload = self._read(n)
                opcode = b1 & 0x0F
                if opcode == 9:
                    self._send(10, payload)
                elif opcode == 8:
                    raise ConnectionError("fermée par le serveur")
                elif opcode == 1:
                    return json.loads(payload)
        except (socket.timeout, TimeoutError):
            return None

    def close(self):
        try:
            self._send(8, b"")
            self.sock.close()
        except OSError:
            pass


def messages(ws, accept, timeout):
    """Messages reçus jusqu'à ce que accept(liste) soit vrai, ou jusqu'au délai."""
    got, deadline = [], time.time() + timeout
    while time.time() < deadline and not accept(got):
        msg = ws.recv(max(0.5, deadline - time.time()))
        if msg is not None:
            got.append(msg)
    return got


@scenario("22. Suivi en temps réel (API Gateway WebSocket)")
def realtime():
    url = HEALTH.get("realtime_url", "")
    if MODE != "aws":
        assert not url, "realtime_url en local"
        return "non applicable en local (pas de WebSocket dans LocalStack gratuit) : le front suit par polling"
    assert url.startswith("wss://"), f"realtime_url : {url!r}"
    shop = HEALTH["backend"]

    bad = WebSocket(f"{url}?shop={shop}&token=abc.def.ghi")
    assert bad.status != 101, "jeton forgé accepté à la connexion"
    bad.close()
    assert WebSocket(f"{url}?shop=autre").status != 101, "paramètre shop invalide accepté"
    info(f"connexions refusées : jeton forgé ({bad.status}), solution inconnue")

    pid = new_product(stock=3, prefix="ZM")
    ws = WebSocket(f"{url}?shop={shop}")
    assert ws.status == 101, f"connexion anonyme : {ws.status}"
    # Abonnement avant le paiement : le workflow démarre dès la confirmation.
    code, created = order(pid, 1, pay=False)
    assert code == 201, f"commande : {code} {created}"
    oid = created["order_id"]
    ws.send({"action": "subscribe", "order_id": oid})
    time.sleep(1)
    assert testpay.pay(BASE, created)[0] == 200, "paiement"
    got = messages(ws, lambda m: any(x.get("event") == "ORDER_DELIVERED" for x in m), timeout=90)
    ws.close()
    assert {"subscribed": oid} in got, f"pas d'accusé d'abonnement : {got}"
    events = [m["event"] for m in got if m.get("type") == "order" and m["order"]["order_id"] == oid]
    assert {"ORDER_PAID", "ORDER_PACKED", "ORDER_SHIPPED", "ORDER_IN_TRANSIT", "ORDER_DELIVERED"} <= set(events), \
        f"événements poussés : {events}"
    shipped = next(m["order"] for m in got if m.get("event") == "ORDER_SHIPPED")
    assert shipped["status"] == "SHIPPED" and shipped.get("shipping"), f"commande poussée : {shipped}"
    info(f"{oid} : {', '.join(events)} reçus sans polling (transporteur {shipped['shipping']['carrier']})")

    # Client connecté : alerte de retour en stock poussée sur sa connexion.
    wished = new_product(stock=0, prefix="ZM")
    assert call("PUT", f"/me/wishlist/{wished}", role="client")[0] == 200
    until(lambda: wished in [p["id"] for p in call("GET", "/me/wishlist", role="client")[1]["products"]])
    ws = WebSocket(f"{url}?shop={shop}&token={urllib.parse.quote(ACCOUNTS.token('client'))}")
    assert ws.status == 101, f"connexion avec jeton : {ws.status}"
    time.sleep(1)
    code, _ = call("POST", "/admin/stock/movements", {"product_id": wished, "warehouse": "PAR", "type": "RESTOCK",
                                                      "quantity": 2}, role="admin")
    assert code == 201
    got = messages(ws, lambda m: any(x.get("type") == "notification" for x in m), timeout=40)
    ws.close()
    notes = [m["notification"] for m in got if m.get("type") == "notification"]
    assert notes and notes[0]["product_id"] == wished, f"alerte poussée : {got}"
    info(f"alerte poussée au client : « {notes[0]['message']} »")
    return "statuts de commande et alerte de retour en stock poussés ; jeton forgé refusé"


if __name__ == "__main__":
    for run in (images, realtime):  # comptes de test supprimés à la sortie (testauth, atexit)
        run()
    for r in results:
        print(f"RESULT:{r}")
    sys.exit(1 if any(r.startswith("❌") for r in results) else 0)
