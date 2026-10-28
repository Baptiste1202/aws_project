// Front commun aux trois solutions : toutes les requêtes passent par /api/*,
// routé par CloudFront vers la Lambda (serverless), l'ALB (VM) ou les deux (hybride).

const $ = (sel) => document.querySelector(sel);
const euro = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
const WAREHOUSES = { PAR: "Paris", LYS: "Lyon" };

// Les textes produits viennent du back-office (et des imports CSV) : toujours échappés.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const store = {
  get(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* stockage indisponible */ }
  },
};

let products = [];
let cart = store.get("cart", []); // [{product_id, size, quantity}]
// Identifiant du panier : le stock des articles y est réservé pendant 15 min.
let cartId = store.get("cartId", null) || newCartId();
let holds = {}; // produit -> expiration de sa réservation
let pollTimer = null;
let health = {}; // /api/health : solution, images_url, realtime_url
let live = null; // WebSocket du suivi en temps réel (AWS seulement)
let payment = null; // paiement en cours : {order, client_secret}

function newCartId() {
  const id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/[^A-Za-z0-9-]/g, "");
  store.set("cartId", id);
  return id;
}

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...Auth.header(), ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Erreur ${res.status}`);
  return body;
}

// ---------------------------------------------------------------- visuels

function illustration(category, hex) {
  const stroke = "rgba(0,0,0,0.25)";
  if (category === "tshirt") {
    return `<svg viewBox="0 0 120 110"><path d="M38 8 L20 14 L4 34 L20 46 L28 38 L28 104 L92 104 L92 38 L100 46 L116 34 L100 14 L82 8 C78 18 70 22 60 22 C50 22 42 18 38 8 Z" fill="${hex}" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/></svg>`;
  }
  if (category === "pull") {
    return `<svg viewBox="0 0 120 110"><path d="M40 8 L22 14 L10 30 L4 96 L18 98 L26 46 L28 104 L92 104 L94 46 L102 98 L116 96 L110 30 L98 14 L80 8 C76 16 68 20 60 20 C52 20 44 16 40 8 Z" fill="${hex}" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/><path d="M28 96 H92 M4 90 L18 92 M116 90 L102 92" stroke="${stroke}" stroke-width="2"/></svg>`;
  }
  return `<svg viewBox="0 0 140 80"><path d="M10 56 C10 40 14 30 22 26 L52 22 C60 30 70 34 84 36 L112 42 C124 45 130 50 130 58 L130 62 L10 62 Z" fill="${hex}" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/><path d="M8 62 H132 V70 C132 73 130 74 127 74 H13 C10 74 8 73 8 70 Z" fill="#f2f0eb" stroke="${stroke}" stroke-width="2"/><path d="M48 30 L60 40 M56 28 L68 38 M64 30 L74 38" stroke="${stroke}" stroke-width="2"/></svg>`;
}

// ---------------------------------------------------------------- catalogue

// Recherche et filtres : catégorie + formulaire (texte, prix, disponibilité, tri).
function catalogQuery(category) {
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  for (const [k, v] of new FormData($("#search"))) if (v) params.set(k, v);
  return params.toString();
}

async function loadCatalog(category = "") {
  $("#catalog-error").hidden = true;
  try {
    const query = catalogQuery(category);
    const data = await api(`/products${query ? `?${query}` : ""}`);
    if (!query) products = data.products;
    else if (!products.length) products = (await api("/products")).products;
    renderCatalog(data.products);
  } catch (e) {
    $("#catalog-error").textContent = `Impossible de charger le catalogue : ${e.message}`;
    $("#catalog-error").hidden = false;
  }
}

function warehouses(p) {
  const by = p.stock_by_warehouse || {};
  return Object.keys(WAREHOUSES).map((wh) => `${WAREHOUSES[wh]} ${by[wh]?.available ?? 0}`).join(" · ");
}

// Miniature si le produit a une image (servie sous /images/ sur AWS), sinon le dessin.
// Une miniature pas encore prête ou introuvable est retirée : le dessin reste visible.
function visual(p) {
  const svg = illustration(p.category, /^#[0-9a-fA-F]{3,8}$/.test(p.hex) ? p.hex : "#cccccc");
  if (!p.image_id || !health.images_url || !/^[0-9a-f]{32}$/.test(p.image_id)) return svg;
  return `${svg}<img src="${esc(health.images_url)}/${esc(p.id)}/${p.image_id}-480.webp" alt="${esc(p.name)}" loading="lazy">`;
}
document.addEventListener("error", (e) => { if (e.target.matches?.(".visual img")) e.target.remove(); }, true);

function renderCatalog(list) {
  $("#catalog").innerHTML = list.map((p) => `
    <article class="card">
      <div class="visual">${visual(p)}</div>
      <div class="body">
        <h3>${esc(p.name)}</h3>
        <p class="desc">${esc(p.description)}</p>
        <div class="meta">
          <span class="price">${euro.format(p.price)}</span>
          <span class="stock ${p.stock < 25 ? "low" : ""}">${p.stock > 0 ? `${p.stock} en stock` : "Épuisé"} · ${esc(p.color)}</span>
        </div>
        <p class="warehouses muted">${warehouses(p)}</p>
        <p class="rating">${p.rating_count ? `★ ${Number(p.rating_avg).toLocaleString("fr-FR")} (${p.rating_count} avis)` : "Pas encore d'avis"}
          <button class="link" data-reviews="${esc(p.id)}">Avis</button>
          ${Auth.user() ? `<button class="link" data-wish="${esc(p.id)}">♡ Souhaiter</button>` : ""}</p>
        <div class="actions">
          <select aria-label="Taille" data-size="${esc(p.id)}">
            ${p.sizes.map((s) => `<option>${esc(s)}</option>`).join("")}
          </select>
          <button class="primary" data-add="${esc(p.id)}" ${p.stock > 0 ? "" : "disabled"}>Ajouter</button>
        </div>
      </div>
    </article>`).join("");
}

// ---------------------------------------------------------------- panier

function saveCart() {
  store.set("cart", cart);
  renderCart();
}

const quantityOf = (productId) => cart.filter((l) => l.product_id === productId).reduce((n, l) => n + l.quantity, 0);

// Réserve côté serveur la quantité totale du produit dans le panier. Si le stock
// manque (409), le panier revient à son état précédent.
async function reserve(productId, previous) {
  try {
    const r = await api(`/carts/${cartId}/reservations`, {
      method: "POST",
      body: JSON.stringify({ product_id: productId, quantity: quantityOf(productId) }),
    });
    if (r.expires_at) holds[productId] = r.expires_at;
    else delete holds[productId];
    $("#cart-error").hidden = true;
  } catch (e) {
    cart = previous;
    $("#cart-error").textContent = `Réservation impossible : ${e.message}`;
    $("#cart-error").hidden = false;
  }
  saveCart();
  loadCatalog(currentCategory());
}

async function changeCart(productId, mutate) {
  const previous = cart.map((l) => ({ ...l }));
  mutate();
  saveCart();
  await reserve(productId, previous);
}

function addToCart(productId, size) {
  return changeCart(productId, () => {
    const line = cart.find((l) => l.product_id === productId && l.size === size);
    if (line) line.quantity = Math.min(line.quantity + 1, 10);
    else cart.push({ product_id: productId, size, quantity: 1 });
  });
}

function renderCart() {
  const byId = Object.fromEntries(products.map((p) => [p.id, p]));
  cart = cart.filter((l) => byId[l.product_id] || products.length === 0);
  const count = cart.reduce((n, l) => n + l.quantity, 0);
  const total = cart.reduce((t, l) => t + (byId[l.product_id]?.price ?? 0) * l.quantity, 0);

  $("#cart-count").textContent = count;
  const fee = shippingFee($("#shipping-method").value, total);
  $("#cart-total").textContent = euro.format(total + fee);
  $("#shipping-note").textContent = cart.length ? `dont livraison ${fee ? euro.format(fee) : "offerte"}` : "";
  $("#cart-empty").hidden = cart.length > 0;
  $("#checkout").hidden = cart.length === 0;
  $("#cart-lines").innerHTML = cart.map((l, i) => {
    const p = byId[l.product_id];
    if (!p) return "";
    return `<li>
      <div><strong>${esc(p.name)}</strong><br><span class="muted">Taille ${esc(l.size)} · ${euro.format(p.price)}</span></div>
      <div class="qty">
        <button data-dec="${i}" aria-label="Retirer un">−</button>${l.quantity}<button data-inc="${i}" aria-label="Ajouter un">+</button>
      </div>
    </li>`;
  }).join("");

  const expiries = Object.values(holds).map((at) => new Date(at)).sort((a, b) => a - b);
  $("#cart-hold").hidden = cart.length === 0 || expiries.length === 0;
  if (expiries.length) {
    $("#cart-hold").textContent = `Articles réservés jusqu'à ${expiries[0].toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}.`;
  }
}

function openDrawer(open) {
  $("#drawer").classList.toggle("open", open);
  $("#drawer").setAttribute("aria-hidden", String(!open));
  $("#backdrop").hidden = !open;
}

// Frais de port affichés (le serveur les recalcule : shop/domain.py, shipping_fee).
function shippingFee(method, merchandise) {
  const m = (health.shipping_methods || {})[method];
  if (!m) return 0;
  return m.free_from !== null && merchandise >= Number(m.free_from) ? 0 : Number(m.fee);
}

async function checkout(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const button = $("#checkout-submit");
  button.disabled = true;
  $("#checkout-error").hidden = true;
  try {
    const order = await api("/orders", {
      method: "POST",
      body: JSON.stringify({
        customer: { name: form.get("name"), email: form.get("email"), address: form.get("address") || undefined },
        items: cart,
        cart_id: cartId, // la commande consomme les réservations du panier
        promo_code: form.get("promo_code") || undefined,
        shipping_method: form.get("shipping_method"),
      }),
    });
    // Le stock est maintenant réservé à la commande (jusqu'au paiement) : panier vidé.
    cart = [];
    holds = {};
    cartId = newCartId();
    saveCart();
    openDrawer(false);
    store.set("lastOrder", order.order_id);
    showTracking(order);
    loadCatalog(currentCategory());
    if (order.status === "PENDING_PAYMENT" && order.payment?.client_secret) {
      payment = { order, client_secret: order.payment.client_secret };
      openPayment();
    }
  } catch (e) {
    $("#checkout-error").textContent = e.message;
    $("#checkout-error").hidden = false;
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- suivi en temps réel

const STEPS = ["CONFIRMED", "PACKED", "SHIPPED", "IN_TRANSIT", "DELIVERED"];
const RETURNS = ["RETURN_REQUESTED", "RETURNED"];  // lot 3b : après la livraison
const DONE = ["DELIVERED", "CANCELLED", "RETURNED"];

function showTracking(order) {
  $("#tracking").hidden = false;
  $("#tracking-id").textContent = order.order_id;
  const n = order.items.reduce((t, i) => t + i.quantity, 0);
  const promo = Number(order.discount) > 0 ? ` (code ${order.promo_code} : −${euro.format(order.discount)})` : "";
  const fee = Number(order.shipping_fee) > 0 ? ` dont livraison ${euro.format(order.shipping_fee)}` : "";
  $("#tracking-summary").textContent = `${n} article${n > 1 ? "s" : ""} · ${euro.format(order.total)}${fee}${promo} · traitée par la solution ${order.backend}`;

  const reached = RETURNS.includes(order.status) ? STEPS.length - 1 : STEPS.indexOf(order.status);
  const times = Object.fromEntries((order.history || []).map((h) => [h.status, h.at]));
  document.querySelectorAll("#timeline li").forEach((li, i) => {
    li.classList.toggle("done", i <= reached);
    li.classList.toggle("current", i === reached + 1);
    const at = times[li.dataset.status];
    li.querySelector("span").textContent = at ? new Date(at).toLocaleTimeString("fr-FR") : "";
  });

  const ship = $("#tracking-shipping");
  ship.hidden = !order.shipping;
  if (order.shipping) {
    ship.textContent = `Expédiée avec ${order.shipping.carrier} — suivi n° ${order.shipping.tracking_number}`;
  }

  // Annulable tant que la commande n'est pas expédiée (le stock est alors remis).
  const cancelled = order.status === "CANCELLED";
  $("#tracking-cancelled").hidden = !cancelled;
  $("#tracking-cancel").hidden = !["PENDING_PAYMENT", "CONFIRMED", "PACKED"].includes(order.status);
  $("#tracking-cancel").dataset.order = order.order_id;
  const pending = order.status === "PENDING_PAYMENT";
  $("#tracking-pending").hidden = !pending;
  $("#tracking-pay").hidden = !(pending && payment?.order.order_id === order.order_id);
  // Facture : réservée au client connecté qui a passé la commande, une fois payée.
  const mine = Auth.user() && order.user_id === Auth.user().sub;
  $("#tracking-invoice").hidden = !(mine && [...STEPS, ...RETURNS].includes(order.status));
  $("#tracking-invoice").dataset.order = order.order_id;
  // Retour (lot 3b) : 14 jours après la livraison, remboursé à réception du colis.
  const ret = order.return || {};
  const deliveredAt = times.DELIVERED ? new Date(times.DELIVERED).getTime() : 0;
  $("#return-form").hidden = !(mine && order.status === "DELIVERED" && Date.now() - deliveredAt < 14 * 864e5);
  $("#tracking-return-btn").dataset.order = order.order_id;
  const info = $("#tracking-return");
  info.hidden = !RETURNS.includes(order.status);
  if (order.status === "RETURN_REQUESTED") {
    info.textContent = `Retour enregistré — étiquette ${ret.label} : déposez le colis en point relais. Remboursement de ${euro.format(order.total)} à réception.`;
  } else if (order.status === "RETURNED") {
    const refunded = order.payment?.status === "refunded";
    info.textContent = `Retour reçu le ${new Date(ret.received_at).toLocaleDateString("fr-FR")} — ${refunded ? `remboursé : ${euro.format(order.total)}` : "remboursement en cours"}.`;
  }

  clearTimeout(pollTimer);
  if (!DONE.includes(order.status)) {
    if (health.realtime_url) subscribe(order.order_id);
    // Avec le WebSocket ouvert, le polling n'est plus qu'un filet de sécurité (10 s).
    const delay = live?.readyState === WebSocket.OPEN ? 10000 : 2000;
    pollTimer = setTimeout(async () => {
      // En cas d'erreur (réseau, ou 404 juste après la création en solution hybride, le
      // temps que la commande arrive dans DynamoDB), on garde le dernier état affiché
      // (d'abord la réponse du POST) et on réessaie au tour suivant.
      let next = order;
      try { next = await api(`/orders/${order.order_id}`); } catch { /* nouvel essai dans 2 s */ }
      showTracking(next);
    }, delay);
  }
}

// ---------------------------------------------------------------- paiement (lot 3a)
// AWS : Stripe.js (js.stripe.com, imposé par Stripe : la carte ne passe jamais par nos
// serveurs). Local : faux Stripe (local/stripe_local.py), cartes de test sans saisie.
// Après la confirmation chez Stripe, le serveur relit le paiement (confirm-payment).

let stripe = null;
let elements = null;

function loadStripeJs() {
  if (window.Stripe) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://js.stripe.com/v3/";
    s.onload = resolve;
    s.onerror = () => reject(new Error("Stripe.js n'a pas pu être chargé"));
    document.head.appendChild(s);
  });
}

async function openPayment() {
  const conf = health.payment || {};
  $("#pay-amount").textContent = euro.format(payment.order.total);
  $("#pay-error").hidden = true;
  $("#pay-local").hidden = conf.type !== "local";
  $("#pay-submit").hidden = conf.type !== "stripe";
  $("#pay-element").innerHTML = "";
  $("#pay-hint").textContent = conf.type === "stripe" ? "Mode test : carte 4242 4242 4242 4242, date future, CVC quelconque." : "Faux Stripe local (stack Docker).";
  $("#pay-dialog").showModal();
  if (conf.type === "stripe") {
    try {
      await loadStripeJs();
      stripe = stripe || Stripe(conf.publishable_key);
      elements = stripe.elements({ clientSecret: payment.client_secret, locale: "fr" });
      elements.create("payment").mount("#pay-element");
    } catch (e) { payError(e.message); }
  }
}

function payError(message) {
  $("#pay-error").textContent = message;
  $("#pay-error").hidden = false;
}

async function finishPayment() {
  const order = await api(`/orders/${payment.order.order_id}/confirm-payment`, { method: "POST" });
  payment = null;
  $("#pay-dialog").close();
  showTracking(order);
}

$("#pay-submit").addEventListener("click", async () => {
  $("#pay-submit").disabled = true;
  try {
    const { error } = await stripe.confirmPayment({ elements, redirect: "if_required" });
    if (error) return payError(error.message);
    await finishPayment();
  } catch (e) { payError(e.message); } finally { $("#pay-submit").disabled = false; }
});

$("#pay-local").addEventListener("click", async (e) => {
  const card = e.target.dataset.card;
  if (!card) return;
  const conf = health.payment;
  const id = payment.client_secret.split("_secret_")[0];
  try {
    const res = await fetch(`${conf.url}/v1/payment_intents/${id}/confirm`, {
      method: "POST",
      headers: { Authorization: `Bearer ${conf.publishable_key}`, "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_secret: payment.client_secret, payment_method: card }),
    });
    const body = await res.json();
    if (!res.ok) return payError(body.error?.message || `Erreur ${res.status}`);
    await finishPayment();
  } catch (err) { payError(err.message); }
});

$("#pay-cancel").addEventListener("click", () => $("#pay-dialog").close());
$("#tracking-pay").addEventListener("click", openPayment);
$("#tracking-invoice").addEventListener("click", async (e) => {
  try {
    const r = await api(`/orders/${e.target.dataset.order}/invoice`, { method: "POST" });
    window.open(r.url, "_blank", "noopener");
  } catch (err) {
    $("#tracking-error").textContent = err.message;
    $("#tracking-error").hidden = false;
  }
});

// ---------------------------------------------------------------- temps réel (lot 2b)
// API Gateway WebSocket (AWS) : statut de la commande suivie et retours en stock du
// client connecté, poussés par la Lambda notifier. Sans WebSocket (local), polling seul.

let liveOrders = new Set();

function connectLive() {
  if (!health.realtime_url || !window.WebSocket) return null;
  if (live && live.readyState <= WebSocket.OPEN) return live;
  const token = Auth.token();
  const url = `${health.realtime_url}?shop=${encodeURIComponent(health.backend)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  live = new WebSocket(url);
  live.addEventListener("open", () => liveOrders.forEach((id) => live.send(JSON.stringify({ action: "subscribe", order_id: id }))));
  live.addEventListener("message", (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === "order" && msg.order && $("#tracking-id").textContent === msg.order.order_id) {
      showTracking(msg.order);
      if (msg.event === "ORDER_CANCELLED") loadCatalog(currentCategory());
    }
    if (msg.type === "notification") toast(msg.notification.message);
  });
  live.addEventListener("close", () => { live = null; });
  return live;
}

function subscribe(orderId) {
  if (liveOrders.has(orderId) && live) return;
  liveOrders.add(orderId);
  const ws = connectLive();
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ action: "subscribe", order_id: orderId }));
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 6000);
}

function closeTracking() {
  clearTimeout(pollTimer);
  $("#tracking").hidden = true;
}

async function cancelOrder() {
  const button = $("#tracking-cancel");
  button.disabled = true;
  $("#tracking-error").hidden = true;
  try {
    showTracking(await api(`/orders/${button.dataset.order}/cancel`, { method: "POST" }));
    loadCatalog(currentCategory());
  } catch (e) {
    $("#tracking-error").textContent = e.message;
    $("#tracking-error").hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function requestReturn(e) {
  const button = e.target;
  button.disabled = true;
  $("#tracking-error").hidden = true;
  try {
    showTracking(await api(`/orders/${button.dataset.order}/return`, {
      method: "POST", body: JSON.stringify({ reason: $("#return-reason").value }),
    }));
  } catch (err) {
    $("#tracking-error").textContent = err.message;
    $("#tracking-error").hidden = false;
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- compte (lot 2a)

let signupMode = false;

function renderAccountButton() {
  const user = Auth.user();
  $("#account-button").textContent = user ? `Mon compte (${user.name})` : "Se connecter";
}

function openLogin() {
  signupMode = false;
  renderLoginMode();
  $("#login-error").hidden = true;
  $("#login-dialog").showModal();
}

function renderLoginMode(confirming = false) {
  $("#login-title").textContent = confirming ? "Confirmer le compte" : signupMode ? "Créer un compte" : "Connexion";
  $("#login-name").hidden = !signupMode || confirming;
  $("#login-code").hidden = !confirming;
  $("#login-submit").textContent = confirming ? "Confirmer" : signupMode ? "Créer le compte" : "Se connecter";
  $("#login-switch").textContent = signupMode ? "J'ai déjà un compte" : "Créer un compte";
  $("#login-form").dataset.confirming = confirming ? "1" : "";
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  $("#login-error").hidden = true;
  try {
    if (e.target.dataset.confirming) await Auth.confirm(f.username, f.code.trim(), f.password);
    else if (signupMode) {
      // Cognito envoie un code par e-mail ; le fournisseur local connecte directement.
      if (await Auth.signup(f.username, f.password, f.name)) return renderLoginMode(true);
    } else await Auth.login(f.username, f.password);
    $("#login-dialog").close();
    renderAccountButton();
    loadCatalog(currentCategory());
  } catch (err) {
    $("#login-error").textContent = err.message;
    $("#login-error").hidden = false;
  }
});
$("#login-switch").addEventListener("click", () => { signupMode = !signupMode; renderLoginMode(); });
$("#login-cancel").addEventListener("click", () => $("#login-dialog").close());

async function openAccount() {
  const user = Auth.user();
  if (!user) return openLogin();
  $("#account-name").textContent = `${user.email}${user.groups.length ? ` · ${user.groups.join(", ")}` : ""}`;
  $("#account").showModal();
  const [notes, wishes, orders] = await Promise.all([
    api("/me/notifications").catch(() => ({ notifications: [] })),
    api("/me/wishlist").catch(() => ({ products: [] })),
    api("/me/orders").catch(() => ({ orders: [] })),
  ]);
  $("#notifications").innerHTML = notes.notifications.map((n) =>
    `<li>${new Date(n.at).toLocaleString("fr-FR")} — ${esc(n.message)}</li>`).join("") || "<li class=\"muted\">Aucune alerte.</li>";
  $("#wishlist").innerHTML = wishes.products.map((p) =>
    `<li>${esc(p.name)} — ${p.stock > 0 ? `${p.stock} en stock` : "épuisé (alerte au retour en stock)"}
     <button class="link" data-unwish="${esc(p.id)}">Retirer</button></li>`).join("") || "<li class=\"muted\">Liste vide.</li>";
  $("#my-orders").innerHTML = orders.orders.map((o) =>
    `<li>${esc(o.order_id)} — ${new Date(o.created_at).toLocaleDateString("fr-FR")} — ${euro.format(o.total)} — ${esc(o.status)}</li>`)
    .join("") || "<li class=\"muted\">Aucune commande.</li>";
}

async function wish(productId, button) {
  try {
    await api(`/me/wishlist/${productId}`, { method: "PUT" });
    if (button) button.textContent = "♥ Souhaité";
  } catch (e) {
    if (button) button.textContent = e.message;
  }
}

$("#wishlist").addEventListener("click", async (e) => {
  const id = e.target.dataset.unwish;
  if (!id) return;
  await api(`/me/wishlist/${id}`, { method: "DELETE" }).catch(() => {});
  e.target.closest("li").remove();
});
$("#account-button").addEventListener("click", openAccount);
$("#account-close").addEventListener("click", () => $("#account").close());
$("#logout").addEventListener("click", () => {
  Auth.logout();
  $("#account").close();
  renderAccountButton();
  loadCatalog(currentCategory());
});

// ---------------------------------------------------------------- avis

async function openReviews(productId) {
  const p = products.find((x) => x.id === productId);
  $("#reviews-product").textContent = p ? p.name : productId;
  $("#review-form").dataset.product = productId;
  $("#review-form").hidden = !Auth.user();
  $("#review-status").textContent = Auth.user() ? "" : "Connectez-vous pour laisser un avis (après avoir commandé le produit).";
  $("#reviews").showModal();
  const { reviews } = await api(`/products/${productId}/reviews`).catch(() => ({ reviews: [] }));
  $("#reviews-list").innerHTML = reviews.map((r) =>
    `<li><strong>${"★".repeat(r.rating)}${"☆".repeat(5 - r.rating)}</strong> ${esc(r.author)} — ${esc(r.comment)}</li>`)
    .join("") || "<li class=\"muted\">Pas encore d'avis.</li>";
}

$("#review-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  try {
    await api(`/products/${e.target.dataset.product}/reviews`, {
      method: "POST", body: JSON.stringify({ rating: Number(f.rating), comment: f.comment }),
    });
    $("#review-status").textContent = "Merci pour votre avis.";
    setTimeout(() => { openReviews(e.target.dataset.product); loadCatalog(currentCategory()); }, 1000);
  } catch (err) {
    $("#review-status").textContent = err.message;
  }
});
$("#reviews-close").addEventListener("click", () => $("#reviews").close());

// ---------------------------------------------------------------- stats Kafka

async function loadStats() {
  try {
    const s = await api("/stats");
    if (!s.enabled) return;
    $("#stats").hidden = false;
    $("#stat-orders").textContent = s.orders;
    $("#stat-shipped").textContent = s.shipped;
    $("#stat-revenue").textContent = euro.format(s.revenue);
    $("#stat-recent").innerHTML = s.recent
      .map((e) => `<li>${new Date(e.at).toLocaleTimeString("fr-FR")} — ${e.type} — ${e.order_id} (${e.source})</li>`)
      .join("");
    setTimeout(loadStats, 3000);
  } catch { /* stats indisponibles */ }
}

// ---------------------------------------------------------------- initialisation

function currentCategory() {
  return document.querySelector("#filters .active")?.dataset.category ?? "";
}

$("#filters").addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  document.querySelectorAll("#filters button").forEach((b) => b.classList.toggle("active", b === button));
  loadCatalog(button.dataset.category);
});

$("#search").addEventListener("submit", (e) => {
  e.preventDefault();
  loadCatalog(currentCategory());
});

$("#catalog").addEventListener("click", (e) => {
  if (e.target.dataset.reviews) return openReviews(e.target.dataset.reviews);
  if (e.target.dataset.wish) return wish(e.target.dataset.wish, e.target);
  const id = e.target.dataset.add;
  if (!id) return;
  addToCart(id, document.querySelector(`select[data-size="${id}"]`).value);
  e.target.textContent = "Ajouté ✓";
  setTimeout(() => { e.target.textContent = "Ajouter"; }, 900);
});

$("#cart-lines").addEventListener("click", (e) => {
  const inc = e.target.dataset.inc;
  const dec = e.target.dataset.dec;
  const i = inc ?? dec;
  if (i === undefined || !cart[i]) return;
  changeCart(cart[i].product_id, () => {
    if (inc !== undefined) cart[i].quantity = Math.min(cart[i].quantity + 1, 10);
    else {
      cart[i].quantity -= 1;
      if (cart[i].quantity <= 0) cart.splice(i, 1);
    }
  });
});

$("#cart-button").addEventListener("click", () => openDrawer(true));
$("#drawer-close").addEventListener("click", () => openDrawer(false));
$("#backdrop").addEventListener("click", () => openDrawer(false));
$("#tracking-close").addEventListener("click", closeTracking);
$("#tracking-cancel").addEventListener("click", cancelOrder);
$("#tracking-return-btn").addEventListener("click", requestReturn);
$("#checkout").addEventListener("submit", checkout);
$("#shipping-method").addEventListener("change", renderCart);

(async function init() {
  try {
    health = await api("/health");
    $("#backend").textContent = `${health.backend} · ${health.region}`;
  } catch { $("#backend").textContent = "hors ligne"; }
  await Auth.init();
  renderAccountButton();
  if (Auth.user()) connectLive(); // alertes de retour en stock
  await loadCatalog();
  renderCart();
  // Panier conservé d'une visite précédente : ses réservations ont pu expirer, on
  // les renouvelle ; un produit qui n'est plus disponible est retiré du panier.
  for (const pid of [...new Set(cart.map((l) => l.product_id))]) {
    await reserve(pid, cart.filter((l) => l.product_id !== pid));
  }
  loadStats();
  const last = store.get("lastOrder", null);
  if (last) {
    try {
      const order = await api(`/orders/${last}`);
      if (!DONE.includes(order.status)) showTracking(order);
    } catch { store.set("lastOrder", null); }
  }
})();
