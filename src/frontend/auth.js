// Connexion commune à la boutique et au back-office.
// La configuration vient de GET /api/health (champ "auth") :
//   - cognito : API publique d'Amazon Cognito (InitiateAuth, SignUp, ConfirmSignUp) ;
//   - local   : fournisseur d'identité de la stack Docker (/token, /signup).
// Le jeton d'identité (JWT) est gardé pour la session du navigateur et envoyé à l'API
// dans l'en-tête Authorization ; l'API vérifie sa signature et son émetteur.

const Auth = (() => {
  let config = { type: "none" };
  let token = null;
  try { token = sessionStorage.getItem("idToken"); } catch { /* stockage indisponible */ }

  function claims() {
    if (!token) return null;
    try {
      const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      if (payload.exp * 1000 < Date.now()) { logout(); return null; }
      return payload;
    } catch { return null; }
  }

  function save(t) {
    token = t;
    try { t ? sessionStorage.setItem("idToken", t) : sessionStorage.removeItem("idToken"); } catch { /* idem */ }
  }

  async function cognito(target, body) {
    const res = await fetch(`https://cognito-idp.${config.region}.amazonaws.com/`, {
      method: "POST",
      headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}` },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || `Erreur ${res.status}`);
    return data;
  }

  async function local(path, body) {
    const res = await fetch(`${config.url}${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Erreur ${res.status}`);
    return data;
  }

  async function login(username, password) {
    if (config.type === "cognito") {
      const r = await cognito("InitiateAuth", {
        AuthFlow: "USER_PASSWORD_AUTH", ClientId: config.client_id,
        AuthParameters: { USERNAME: username, PASSWORD: password },
      });
      save(r.AuthenticationResult.IdToken);
    } else if (config.type === "local") {
      save((await local("/token", { username, password })).id_token);
    } else {
      throw new Error("Connexion indisponible");
    }
    return claims();
  }

  // Renvoie true si un code de confirmation (envoyé par e-mail par Cognito) est attendu.
  async function signup(username, password, name) {
    if (config.type === "cognito") {
      await cognito("SignUp", {
        ClientId: config.client_id, Username: username, Password: password,
        UserAttributes: [{ Name: "email", Value: username }, { Name: "name", Value: name || username }],
      });
      return true;
    }
    await local("/signup", { username, password, name });
    await login(username, password);
    return false;
  }

  async function confirm(username, code, password) {
    await cognito("ConfirmSignUp", { ClientId: config.client_id, Username: username, ConfirmationCode: code });
    return login(username, password);
  }

  function logout() { save(null); }

  return {
    async init() {
      try { config = (await (await fetch("/api/health")).json()).auth || config; } catch { /* hors ligne */ }
      return config;
    },
    login, signup, confirm, logout, claims,
    header: () => (claims() ? { Authorization: `Bearer ${token}` } : {}),
    token: () => (claims() ? token : null),
    user: () => {
      const c = claims();
      return c && { sub: c.sub, email: c.email, name: c.name || c.email, groups: c["cognito:groups"] || [] };
    },
  };
})();
