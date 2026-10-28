"""Comptes et jetons pour les scénarios de test (bibliothèque standard seulement).

    from testauth import Accounts
    accounts = Accounts(base_url, mode)       # mode : "local" ou "aws"
    token = accounts.token("admin")           # "admin", "magasinier" ou "client"

La configuration de connexion est lue dans GET /api/health :
  - local : fournisseur d'identité local (POST /dev/users, POST /token) ;
  - aws   : Amazon Cognito. Un utilisateur par rôle est créé pour le test (AWS CLI,
            admin-create-user), avec un mot de passe aléatoire gardé en mémoire et
            jamais écrit ; il est supprimé à la fin du script. Le jeton est obtenu
            par InitiateAuth (USER_PASSWORD_AUTH), l'API publique de Cognito.
"""

import atexit
import json
import secrets
import subprocess
import urllib.error
import urllib.request

GROUPS = {"admin": ["admin"], "magasinier": ["magasinier"], "client": [], "client2": []}


def _post(url, payload, headers=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Accounts:
    def __init__(self, base_url, mode):
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=60) as r:
            self.config = json.load(r)["auth"]
        self.mode = mode
        self.run = secrets.token_hex(4)
        self.tokens, self.users, self.created = {}, {}, []
        atexit.register(self.cleanup)

    def username(self, role):
        # threadly.test (RFC 6761, jamais livrable) plutôt que example.com, que le mailer
        # ignore : les e-mails des clients de test (liste de souhaits) restent vérifiables.
        return f"test-{role}-{self.run}@threadly.test"

    def token(self, role, fresh=False):
        if role not in self.tokens or fresh:
            self.tokens[role] = self._login(role)
        return self.tokens[role]

    def sub(self, role):
        token = self.token(role)
        import base64
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]

    def _login(self, role):
        username = self.username(role)
        password = self.users.get(role) or (secrets.token_urlsafe(12) + "Aa1!")
        self.users[role] = password
        if self.config["type"] == "local":
            url = self.config["url"]
            code, data = _post(f"{url}/dev/users", {"username": username, "password": password,
                                                   "groups": GROUPS[role]})
            assert code == 201, f"compte local {role} : {code} {data}"
            code, data = _post(f"{url}/token", {"username": username, "password": password})
            assert code == 200, f"connexion locale {role} : {code} {data}"
            return data["id_token"]
        if self.config["type"] != "cognito":
            raise AssertionError(f"authentification indisponible : {self.config}")
        pool, region = self.config["user_pool_id"], self.config["region"]
        if role not in self.created:
            self._aws("admin-create-user", "--user-pool-id", pool, "--username", username,
                      "--message-action", "SUPPRESS", "--user-attributes", f"Name=email,Value={username}",
                      "Name=email_verified,Value=true")
            self.created.append(role)  # supprimé à la sortie, même si la suite échoue
            # --password=... : un mot de passe aléatoire qui commence par « - » serait lu
            # comme une option par l'AWS CLI.
            self._aws("admin-set-user-password", "--user-pool-id", pool, "--username", username,
                      f"--password={password}", "--permanent")
            for group in GROUPS[role]:
                self._aws("admin-add-user-to-group", "--user-pool-id", pool, "--username", username,
                          "--group-name", group)
        code, data = _post(f"https://cognito-idp.{region}.amazonaws.com/",
                           {"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": self.config["client_id"],
                            "AuthParameters": {"USERNAME": username, "PASSWORD": password}},
                           {"Content-Type": "application/x-amz-json-1.1",
                            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"})
        assert code == 200, f"connexion Cognito {role} : {code} {data.get('message', data)}"
        return data["AuthenticationResult"]["IdToken"]

    def _aws(self, *args):
        # Le mot de passe passe en argument d'un processus local, jamais dans un fichier.
        out = subprocess.run(["aws", "cognito-idp", *args, "--region", self.config.get("region", "eu-west-3")],
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise AssertionError(f"AWS CLI {args[0]} : {out.stderr.strip()[:200]}")

    def cleanup(self):
        if self.mode == "aws" and self.config.get("type") == "cognito":
            for role in self.created:
                subprocess.run(["aws", "cognito-idp", "admin-delete-user", "--user-pool-id",
                                self.config["user_pool_id"], "--username", self.username(role),
                                "--region", self.config["region"]], capture_output=True)
