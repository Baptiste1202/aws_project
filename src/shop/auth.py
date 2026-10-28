"""Authentification : jetons JWT émis par Amazon Cognito (sur AWS) ou par le
fournisseur d'identité local (stack Docker seulement).

Vérification sans dépendance (la Lambda n'a que la bibliothèque standard et boto3) :
  - algorithme RS256 uniquement (jamais "none", jamais HS256) ;
  - signature RSA PKCS#1 v1.5 / SHA-256 vérifiée avec la clé publique du JWKS de
    l'émetteur configuré, choisie par le « kid » du jeton ;
  - émetteur (iss) égal à celui configuré, jeton non expiré, client (aud ou
    client_id) égal à celui configuré.

Garde-fou : seul un émetteur Cognito (https://cognito-idp.<région>.amazonaws.com/...)
est accepté, sauf si ALLOW_LOCAL_AUTH=1, variable définie uniquement dans
local/docker-compose.yml (jamais par Terraform). Sur AWS, un jeton du fournisseur
local est donc toujours refusé.

Rôles (groupes Cognito) : admin, magasinier ; tout utilisateur connecté est client.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.request

from shop.domain import ApiError

COGNITO_ISSUER = re.compile(r"^https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9_-]+$")
JWKS_TTL = 3600
LEEWAY = 60
# En-tête DER de DigestInfo pour SHA-256 (RFC 8017, EMSA-PKCS1-v1_5).
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url_decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def b64url_int(data):
    return int.from_bytes(b64url_decode(data), "big")


def rsa_verify(message, signature, n, e):
    """Vérifie une signature RSASSA-PKCS1-v1_5 avec SHA-256 (comparaison à temps constant)."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    digest = SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(digest) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(em, expected)


class Authenticator:
    def __init__(self, issuer, client_id, jwks_url=None, allow_local=False):
        self.issuer = (issuer or "").rstrip("/")
        self.client_id = client_id or ""
        self.jwks_url = jwks_url or f"{self.issuer}/.well-known/jwks.json"
        # Garde-fou : un émetteur non Cognito n'est accepté qu'en local.
        self.enabled = bool(self.issuer and self.client_id) and (
            bool(COGNITO_ISSUER.match(self.issuer)) or allow_local)
        self._keys, self._fetched = {}, 0

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("AUTH_ISSUER"), os.environ.get("AUTH_CLIENT_ID"),
                   os.environ.get("AUTH_JWKS_URL"), allow_local=os.environ.get("ALLOW_LOCAL_AUTH") == "1")

    def _key(self, kid):
        if kid not in self._keys or time.time() - self._fetched > JWKS_TTL:
            with urllib.request.urlopen(self.jwks_url, timeout=5) as r:
                jwks = json.load(r)
            self._keys = {k["kid"]: (b64url_int(k["n"]), b64url_int(k["e"]))
                          for k in jwks.get("keys", []) if k.get("kty") == "RSA"}
            self._fetched = time.time()
        return self._keys.get(kid)

    def verify(self, token):
        """Renvoie les attributs du jeton s'il est valide, lève 401 sinon."""
        if not self.enabled:
            raise ApiError(401, "Authentification indisponible")
        try:
            header_b64, payload_b64, sig_b64 = token.split(".")
            header = json.loads(b64url_decode(header_b64))
            claims = json.loads(b64url_decode(payload_b64))
            signature = b64url_decode(sig_b64)
        except (ValueError, json.JSONDecodeError):
            raise ApiError(401, "Jeton invalide")
        if header.get("alg") != "RS256":
            raise ApiError(401, "Jeton invalide : algorithme refusé")
        key = self._key(header.get("kid", ""))
        if not key or not rsa_verify(f"{header_b64}.{payload_b64}".encode(), signature, *key):
            raise ApiError(401, "Jeton invalide : signature")
        now = time.time()
        if claims.get("iss") != self.issuer:
            raise ApiError(401, "Jeton invalide : émetteur")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] + LEEWAY < now:
            raise ApiError(401, "Jeton expiré")
        audience = claims.get("aud") if claims.get("token_use") == "id" else claims.get("client_id")
        if claims.get("token_use") not in ("id", "access") or audience != self.client_id:
            raise ApiError(401, "Jeton invalide : client")
        return claims

    def user(self, headers):
        """Utilisateur de l'en-tête Authorization (Bearer), ou None sans en-tête."""
        value = (headers or {}).get("authorization", "")
        if not value:
            return None
        if not value.lower().startswith("bearer "):
            raise ApiError(401, "En-tête Authorization invalide")
        claims = self.verify(value[7:].strip())
        groups = claims.get("cognito:groups") or []
        return {"sub": claims["sub"], "email": claims.get("email", ""),
                "name": claims.get("name") or claims.get("email", "").split("@")[0],
                "groups": groups if isinstance(groups, list) else [groups]}

    def public_config(self):
        """Ce dont le front a besoin pour se connecter (aucun secret)."""
        if not self.enabled:
            return {"type": "none"}
        if COGNITO_ISSUER.match(self.issuer):
            region, pool = self.issuer.split("cognito-idp.")[1].split(".amazonaws.com/")
            return {"type": "cognito", "region": region, "user_pool_id": pool, "client_id": self.client_id}
        return {"type": "local", "url": os.environ.get("AUTH_PUBLIC_URL", ""), "client_id": self.client_id}


def require(user, *roles):
    """Contrôle d'accès : 401 sans utilisateur, 403 sans le rôle demandé."""
    if not user:
        raise ApiError(401, "Connexion requise")
    if roles and not set(roles) & set(user["groups"]):
        raise ApiError(403, "Droits insuffisants pour cette action")
    return user
