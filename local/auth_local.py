"""Fournisseur d'identité LOCAL (stack Docker seulement), à la place de Cognito.

LocalStack gratuit ne propose pas Cognito. Ce service émet des jetons de même forme
que Cognito (RS256, kid, iss, aud, token_use, cognito:groups), signés par une clé RSA
générée au démarrage. Il n'est lancé que par local/docker-compose.yml : Terraform n'y
fait aucune référence, et le code de l'API n'accepte un émetteur non Cognito que si
ALLOW_LOCAL_AUTH=1 (défini uniquement dans la stack locale). Voir src/shop/auth.py.

  GET  /.well-known/jwks.json
  POST /token        {"username", "password"}            -> id_token, access_token
  POST /signup       {"username", "password", "name"?}   -> compte client
  POST /dev/users    {"username", "password", "groups"}  -> compte avec rôles (tests)

Bibliothèque standard seulement. Les fonctions de clé et de signature servent aussi au
script de test pour forger des jetons invalides.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ISSUER = os.environ.get("AUTH_ISSUER", "http://auth-local:9000")
CLIENT_ID = os.environ.get("AUTH_CLIENT_ID", "local-client")
PORT = int(os.environ.get("PORT", "9000"))
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


# ---------------------------------------------------------------------------
# RSA en Python pur (clé de 2048 bits, signature PKCS#1 v1.5 / SHA-256)
# ---------------------------------------------------------------------------

def _probably_prime(n, rounds=40):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _probably_prime(candidate):
            return candidate


def generate_key(bits=2048):
    e = 65537
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            n = p * q
            return {"n": n, "e": e, "d": pow(e, -1, phi), "kid": uuid.uuid4().hex}


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64(value):
    return b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def jwk(key):
    return {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": key["kid"], "n": _int_b64(key["n"]),
            "e": _int_b64(key["e"])}


def sign_jwt(claims, key, header=None):
    header = header or {"alg": "RS256", "typ": "JWT", "kid": key["kid"]}
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims).encode())}"
    k = (key["n"].bit_length() + 7) // 8
    digest = SHA256_PREFIX + hashlib.sha256(signing_input.encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(em, "big"), key["d"], key["n"]).to_bytes(k, "big")
    return f"{signing_input}.{b64url(signature)}"


def claims_for(user, issuer=ISSUER, client_id=CLIENT_ID, token_use="id", ttl=3600):
    now = int(time.time())
    claims = {"sub": user["sub"], "iss": issuer, "token_use": token_use, "iat": now, "exp": now + ttl,
              "email": user["username"], "name": user.get("name") or user["username"].split("@")[0],
              "cognito:groups": user.get("groups", [])}
    claims["aud" if token_use == "id" else "client_id"] = client_id
    return claims


# ---------------------------------------------------------------------------
# Service HTTP
# ---------------------------------------------------------------------------

KEY = None
USERS = {}  # username -> {"sub", "username", "password_hash", "groups", "name"}
LOCK = threading.Lock()


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def add_user(username, password, groups=(), name=""):
    username = username.strip().lower()
    if "@" not in username or len(password) < 8:
        raise ValueError("adresse e-mail et mot de passe de 8 caractères au moins requis")
    with LOCK:
        sub = USERS.get(username, {}).get("sub") or str(uuid.uuid5(uuid.NAMESPACE_URL, username))
        USERS[username] = {"sub": sub, "username": username, "salt": sub, "password_hash": _hash(password, sub),
                           "groups": list(groups), "name": name}
    return USERS[username]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if self.path == "/.well-known/jwks.json":
            return self._send(200, {"keys": [jwk(KEY)]})
        self._send(404, {"error": "inconnu"})

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "JSON invalide"})
        try:
            if self.path == "/token":
                user = USERS.get(str(data.get("username", "")).strip().lower())
                if not user or user["password_hash"] != _hash(str(data.get("password", "")), user["salt"]):
                    return self._send(401, {"error": "Identifiants incorrects"})
                return self._send(200, {"id_token": sign_jwt(claims_for(user), KEY),
                                        "access_token": sign_jwt(claims_for(user, token_use="access"), KEY),
                                        "expires_in": 3600})
            if self.path == "/signup":
                user = add_user(data.get("username", ""), str(data.get("password", "")), name=data.get("name", ""))
                return self._send(201, {"username": user["username"], "sub": user["sub"]})
            if self.path == "/dev/users":
                user = add_user(data.get("username", ""), str(data.get("password", "")), data.get("groups", []))
                return self._send(201, {"username": user["username"], "sub": user["sub"], "groups": user["groups"]})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "inconnu"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    KEY = generate_key()
    # Comptes de démonstration (mots de passe locaux, sans valeur hors de la stack Docker).
    for spec in filter(None, (x.strip() for x in os.environ.get("AUTH_DEMO_USERS", "").split(","))):
        username, password, *groups = spec.split(":")
        add_user(username, password, [g for g in groups if g])
    print(json.dumps({"msg": "fournisseur d'identité local démarré", "issuer": ISSUER, "port": PORT,
                      "comptes": sorted(USERS)}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
