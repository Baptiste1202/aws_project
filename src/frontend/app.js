// Front commun aux deux solutions : toutes les requêtes passent par /api/*,
// routé par CloudFront vers la Lambda (serverless) ou vers l'ALB (VM).

const $ = (sel) => document.querySelector(sel);
const euro = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });

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
let pollTimer = null;

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
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

async function loadCatalog(category = "") {
  $("#catalog-error").hidden = true;
  try {
    const data = await api(`/products${category ? `?category=${category}` : ""}`);
    if (!category) products = data.products;
    renderCatalog(data.products);
  } catch (e) {
    $("#catalog-error").textContent = `Impossible de charger le catalogue : ${e.message}`;
    $("#catalog-error").hidden = false;
  }
}

function renderCatalog(list) {
  $("#catalog").innerHTML = list.map((p) => `
    <article class="card">
      <div class="visual">${illustration(p.category, p.hex)}</div>
      <div class="body">
        <h3>${p.name}</h3>
        <p class="desc">${p.description}</p>
        <div class="meta">
          <span class="price">${euro.format(p.price)}</span>
          <span class="stock ${p.stock < 25 ? "low" : ""}">${p.stock > 0 ? `${p.stock} en stock` : "Épuisé"} · ${p.color}</span>
        </div>
        <div class="actions">
          <select aria-label="Taille" data-size="${p.id}">
            ${p.sizes.map((s) => `<option>${s}</option>`).join("")}
          </select>
          <button class="primary" data-add="${p.id}" ${p.stock > 0 ? "" : "disabled"}>Ajouter</button>
        </div>
      </div>
    </article>`).join("");
}

// ---------------------------------------------------------------- panier

function saveCart() {
  store.set("cart", cart);
  renderCart();
}

function addToCart(productId, size) {
  const line = cart.find((l) => l.product_id === productId && l.size === size);
  if (line) line.quantity = Math.min(line.quantity + 1, 10);
  else cart.push({ product_id: productId, size, quantity: 1 });
  saveCart();
}

function renderCart() {
  const byId = Object.fromEntries(products.map((p) => [p.id, p]));
  cart = cart.filter((l) => byId[l.product_id] || products.length === 0);
  const count = cart.reduce((n, l) => n + l.quantity, 0);
  const total = cart.reduce((t, l) => t + (byId[l.product_id]?.price ?? 0) * l.quantity, 0);

  $("#cart-count").textContent = count;
  $("#cart-total").textContent = euro.format(total);
  $("#cart-empty").hidden = cart.length > 0;
  $("#checkout").hidden = cart.length === 0;
  $("#cart-lines").innerHTML = cart.map((l, i) => {
    const p = byId[l.product_id];
    if (!p) return "";
    return `<li>
      <div><strong>${p.name}</strong><br><span class="muted">Taille ${l.size} · ${euro.format(p.price)}</span></div>
      <div class="qty">
        <button data-dec="${i}" aria-label="Retirer un">−</button>${l.quantity}<button data-inc="${i}" aria-label="Ajouter un">+</button>
      </div>
    </li>`;
  }).join("");
}

function openDrawer(open) {
  $("#drawer").classList.toggle("open", open);
  $("#drawer").setAttribute("aria-hidden", String(!open));
  $("#backdrop").hidden = !open;
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
        customer: { name: form.get("name"), email: form.get("email") },
        items: cart,
      }),
    });
    cart = [];
    saveCart();
    openDrawer(false);
    store.set("lastOrder", order.order_id);
    showTracking(order);
    loadCatalog(currentCategory());
  } catch (e) {
    $("#checkout-error").textContent = e.message;
    $("#checkout-error").hidden = false;
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- suivi en temps réel

const STEPS = ["CONFIRMED", "PACKED", "SHIPPED"];

function showTracking(order) {
  $("#tracking").hidden = false;
  $("#tracking-id").textContent = order.order_id;
  const n = order.items.reduce((t, i) => t + i.quantity, 0);
  $("#tracking-summary").textContent = `${n} article${n > 1 ? "s" : ""} · ${euro.format(order.total)} · traitée par la solution ${order.backend}`;

  const reached = STEPS.indexOf(order.status);
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

  clearTimeout(pollTimer);
  if (order.status !== "SHIPPED") {
    pollTimer = setTimeout(async () => {
      try { showTracking(await api(`/orders/${order.order_id}`)); } catch { /* nouvel essai au prochain tour */ }
    }, 2000);
  }
}

function closeTracking() {
  clearTimeout(pollTimer);
  $("#tracking").hidden = true;
}

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

$("#catalog").addEventListener("click", (e) => {
  const id = e.target.dataset.add;
  if (!id) return;
  addToCart(id, document.querySelector(`select[data-size="${id}"]`).value);
  e.target.textContent = "Ajouté ✓";
  setTimeout(() => { e.target.textContent = "Ajouter"; }, 900);
});

$("#cart-lines").addEventListener("click", (e) => {
  const inc = e.target.dataset.inc;
  const dec = e.target.dataset.dec;
  if (inc !== undefined) cart[inc].quantity = Math.min(cart[inc].quantity + 1, 10);
  if (dec !== undefined) {
    cart[dec].quantity -= 1;
    if (cart[dec].quantity <= 0) cart.splice(dec, 1);
  }
  if (inc !== undefined || dec !== undefined) saveCart();
});

$("#cart-button").addEventListener("click", () => openDrawer(true));
$("#drawer-close").addEventListener("click", () => openDrawer(false));
$("#backdrop").addEventListener("click", () => openDrawer(false));
$("#tracking-close").addEventListener("click", closeTracking);
$("#checkout").addEventListener("submit", checkout);

(async function init() {
  api("/health")
    .then((h) => { $("#backend").textContent = `${h.backend} · ${h.region}`; })
    .catch(() => { $("#backend").textContent = "hors ligne"; });
  await loadCatalog();
  renderCart();
  loadStats();
  const last = store.get("lastOrder", null);
  if (last) {
    try {
      const order = await api(`/orders/${last}`);
      if (order.status !== "SHIPPED") showTracking(order);
    } catch { store.set("lastOrder", null); }
  }
})();
