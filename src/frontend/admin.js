// Back-office commun aux trois solutions : routes /api/admin/*, protégées par compte
// (Cognito sur AWS, fournisseur local en local, voir auth.js) : rôle admin (tout) ou
// magasinier (stock seulement). En hybride, les lectures viennent du modèle de lecture
// DynamoDB : une écriture peut mettre une seconde à y apparaître.

const $ = (sel) => document.querySelector(sel);
const euro = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TYPES = { SALE: "Vente", CANCEL: "Annulation", RESTOCK: "Réappro", BREAKAGE: "Casse", INVENTORY: "Inventaire", RETURN: "Retour" };

let products = [];
const isAdmin = () => (Auth.user()?.groups || []).includes("admin");

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...Auth.header(), ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Erreur ${res.status}`);
  return body;
}

// Après une écriture : en hybride, la lecture vient de la projection (cohérence à
// terme, généralement moins d'une seconde) ; on relit donc deux fois.
function refreshSoon() {
  setTimeout(() => refresh().catch(() => {}), 800);
  setTimeout(() => refresh().catch(() => {}), 3000);
}

function status(el, message, ok = true) {
  el.textContent = message;
  el.className = `status ${ok ? "ok" : "error"}`;
}

// ---------------------------------------------------------------- chargement

async function refresh() {
  const [list, journal, low, valuation, returns, pos, dash] = await Promise.all([
    api("/admin/products"), api("/admin/stock/movements?limit=50"), api("/admin/stock/low"), api("/admin/stock/valuation"),
    api("/admin/returns"), api("/admin/purchase-orders?limit=20"), api("/admin/dashboard"),
  ]);
  renderDashboard(dash);
  renderReturns(returns.orders);
  renderPurchaseOrders(pos.purchase_orders);
  if (isAdmin()) renderPromos((await api("/admin/promos")).promos);
  document.querySelectorAll("[data-role=admin]").forEach((el) => { el.hidden = !isAdmin(); });
  const user = Auth.user();
  $("#whoami").textContent = `Connecté : ${user.email} (${user.groups.join(", ")})`;
  $("#whoami").hidden = false;
  products = list.products;
  renderProducts();
  renderJournal(journal.movements);
  $("#low").textContent = low.products.length
    ? low.products.map((p) => `${p.id} ${p.name} : ${p.stock} (seuil ${p.low_stock_threshold})`).join(" · ")
    : "Aucun produit sous son seuil.";
  const wh = valuation.warehouses;
  $("#valuation").innerHTML = `
    <div><span>${euro.format(valuation.total_value)}</span><small class="muted">valeur totale (${valuation.total_units} unités)</small></div>
    <div><span>${euro.format(wh.PAR.value)}</span><small class="muted">Paris (${wh.PAR.units} unités)</small></div>
    <div><span>${euro.format(wh.LYS.value)}</span><small class="muted">Lyon (${wh.LYS.units} unités)</small></div>`;
}

function renderProducts() {
  $("#products").innerHTML = products.map((p) => {
    const by = p.stock_by_warehouse;
    return `<tr class="${p.archived ? "archived" : ""} ${p.stock < p.low_stock_threshold ? "low" : ""}">
      <td>${esc(p.id)}</td><td>${esc(p.name)}</td><td>${esc(p.category)}</td>
      <td class="num">${euro.format(p.price)}</td><td class="num">${euro.format(p.cost_price)}</td>
      <td class="num">${p.low_stock_threshold}</td><td class="num">${by.PAR.on_hand}</td><td class="num">${by.LYS.on_hand}</td>
      <td class="num stock">${p.stock}</td>
      <td>${p.image_id ? "oui" : "—"}</td>
      <td>${isAdmin() ? `<button class="secondary" data-edit="${esc(p.id)}">Modifier</button>
          <button class="secondary" data-archive="${esc(p.id)}" data-value="${!p.archived}">${p.archived ? "Désarchiver" : "Archiver"}</button>` : ""}</td>
    </tr>`;
  }).join("");
  const options = products.filter((p) => !p.archived)
    .map((p) => `<option value="${esc(p.id)}">${esc(p.id)} — ${esc(p.name)}</option>`).join("");
  $("#movement-product").innerHTML = options;
  const chosen = $("#image-product").value;
  $("#image-product").innerHTML = options;
  if (chosen) $("#image-product").value = chosen;
}

function renderJournal(moves) {
  $("#journal").innerHTML = moves.map((m) => `<tr>
    <td>${new Date(m.at).toLocaleString("fr-FR")}</td><td>${esc(m.product_id)}</td><td>${esc(m.warehouse)}</td>
    <td>${TYPES[m.type] || esc(m.type)}</td><td class="num">${m.delta > 0 ? "+" : ""}${m.delta}</td>
    <td class="num">${m.on_hand_after}</td><td>${esc(m.ref)}</td><td>${esc(m.reason)}</td></tr>`).join("");
}

const REASONS = { size: "Taille", changed_mind: "Changement d'avis", defective: "Défectueux", other: "Autre" };
const date = (iso) => (iso ? new Date(iso).toLocaleString("fr-FR") : "—");

const STATUS = {
  PENDING_PAYMENT: "En attente de paiement", CONFIRMED: "Payée", PACKED: "Emballée", SHIPPED: "Expédiée",
  IN_TRANSIT: "En transit", DELIVERED: "Livrée", CANCELLED: "Annulée", RETURN_REQUESTED: "Retour demandé",
  RETURNED: "Retournée",
};

function renderDashboard(d) {
  $("#dash-kpis").innerHTML = `
    <div><span>${euro.format(d.revenue.day)}</span><small class="muted">chiffre d'affaires du jour (${d.orders.day} commandes)</small></div>
    <div><span>${euro.format(d.revenue.week)}</span><small class="muted">7 derniers jours (${d.orders.week} commandes)</small></div>
    <div><span>${d.out_of_stock.length}</span><small class="muted">rupture(s)</small></div>
    <div><span>${d.low_stock.length}</span><small class="muted">stock(s) bas</small></div>`;
  $("#dash-top").innerHTML = d.top_products.map((p) =>
    `<li>${esc(p.name)} : ${p.quantity} vendu(s), ${euro.format(p.revenue)}</li>`).join("") || "<li>Aucune vente.</li>";
  $("#dash-status").innerHTML = Object.entries(d.orders_by_status).map(([s, n]) =>
    `<li>${STATUS[s] || esc(s)} : ${n}</li>`).join("") || "<li>Aucune commande.</li>";
  $("#dash-stock").innerHTML = [
    ...d.out_of_stock.map((p) => `<li><b>Rupture</b> : ${esc(p.id)} ${esc(p.name)}</li>`),
    ...d.low_stock.map((p) => `<li>${esc(p.id)} ${esc(p.name)} : ${p.stock} (seuil ${p.threshold})</li>`),
  ].join("") || "<li>Aucune rupture, aucun stock bas.</li>";
}

$("#report-now").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    const r = await api("/admin/reports/daily", { method: "POST" });
    status($("#report-status"), `Rapport ${r.file} déposé dans S3 et envoyé par e-mail (${r.sold_24h} article(s) vendu(s) en 24 h).`);
    window.open(r.url, "_blank", "noopener");
  } catch (err) {
    status($("#report-status"), err.message, false);
  } finally {
    e.target.disabled = false;
  }
});

function renderReturns(orders) {
  $("#returns").innerHTML = orders.length ? orders.map((o) => `<tr>
    <td>${esc(o.order_id)}</td><td>${date(o.return.requested_at)}</td><td>${esc(o.customer.email)}</td>
    <td>${o.items.map((i) => `${i.quantity} × ${esc(i.name)}`).join(", ")}</td>
    <td>${REASONS[o.return.reason] || esc(o.return.reason)}</td><td>${esc(o.return.label)}</td>
    <td class="num">${euro.format(o.total)}</td>
    <td><button class="primary" data-receive="${esc(o.order_id)}">Réceptionner</button></td></tr>`).join("")
    : `<tr><td colspan="8" class="muted">Aucun retour en attente.</td></tr>`;
}

function renderPurchaseOrders(pos) {
  $("#purchase-orders").innerHTML = pos.length ? pos.map((p) => `<tr>
    <td>${esc(p.po_id)}</td><td>${date(p.created_at)}</td><td>${esc(p.product_id)} ${esc(p.name)}</td>
    <td>${esc(p.supplier)}</td><td>${esc(p.warehouse)}</td><td class="num">${p.quantity}</td>
    <td>${p.status === "OPEN" ? "En attente de livraison" : "Reçu"}</td><td>${date(p.received_at)}</td></tr>`).join("")
    : `<tr><td colspan="8" class="muted">Aucun bon de commande.</td></tr>`;
}

// ---------------------------------------------------------------- actions

$("#returns").addEventListener("click", async (e) => {
  const id = e.target.dataset.receive;
  if (!id) return;
  e.target.disabled = true;
  try {
    const o = await api(`/admin/returns/${id}/receive`, { method: "POST" });
    const refunded = o.payment?.status === "refunded";
    status($("#returns-status"), `${id} : remis en stock, ${refunded ? `remboursé (${euro.format(o.total)})` : "remboursement à reprendre"}.`, refunded);
    refreshSoon();
  } catch (err) {
    status($("#returns-status"), err.message, false);
    e.target.disabled = false;
  }
});

function renderPromos(promos) {
  $("#promos").innerHTML = promos.map((p) => `<tr>
    <td>${esc(p.code)}</td><td>${p.type === "percent" ? "%" : "€"}</td><td class="num">${p.value}</td>
    <td class="num">${euro.format(p.min_total || 0)}</td><td class="num">${p.uses}${p.max_uses ? ` / ${p.max_uses}` : ""}</td>
    <td>${p.expires_at ? new Date(p.expires_at).toLocaleString("fr-FR") : "—"}</td><td>${p.active ? "oui" : "non"}</td></tr>`).join("");
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await Auth.login($("#login-user").value.trim(), $("#login-password").value);
    await refresh();
    $("#login").hidden = true;
    $("#app").hidden = false;
  } catch (err) {
    status($("#login-status"), err.message, false);
  }
});

$("#promo-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.value = Number(data.value);
  data.min_total = Number(data.min_total || 0);
  data.max_uses = data.max_uses ? Number(data.max_uses) : null;
  data.expires_at = data.expires_at ? new Date(data.expires_at).toISOString() : null;
  try {
    await api("/admin/promos", { method: "POST", body: JSON.stringify(data) });
    status($("#promo-status"), `Code ${data.code.toUpperCase()} créé.`);
    refreshSoon();
  } catch (err) {
    status($("#promo-status"), err.message, false);
  }
});

$("#product-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.sizes = data.sizes.split("|").map((s) => s.trim()).filter(Boolean);
  data.price = Number(data.price);
  data.low_stock_threshold = Number(data.low_stock_threshold || 5);
  if (data.cost_price) data.cost_price = Number(data.cost_price); else delete data.cost_price;
  const exists = products.some((p) => p.id === data.id);
  try {
    if (exists) {
      const { id, ...fields } = data;
      await api(`/admin/products/${id}`, { method: "PUT", body: JSON.stringify(fields) });
    } else {
      await api("/admin/products", { method: "POST", body: JSON.stringify(data) });
    }
    status($("#product-status"), `${data.id} ${exists ? "modifié" : "créé"}.`);
    refreshSoon();
  } catch (err) {
    status($("#product-status"), err.message, false);
  }
});

$("#products").addEventListener("click", async (e) => {
  const edit = e.target.dataset.edit;
  const archive = e.target.dataset.archive;
  if (edit) {
    const p = products.find((x) => x.id === edit);
    const form = $("#product-form");
    for (const f of ["id", "name", "category", "description", "price", "cost_price", "low_stock_threshold", "color", "hex"]) {
      form.elements[f].value = p[f];
    }
    form.elements.sizes.value = p.sizes.join("|");
    form.scrollIntoView({ behavior: "smooth" });
  }
  if (archive) {
    try {
      await api(`/admin/products/${archive}/archive`, { method: "POST", body: JSON.stringify({ archived: e.target.dataset.value === "true" }) });
      refreshSoon();
    } catch (err) {
      status($("#product-status"), err.message, false);
    }
  }
});

$("#movement-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.quantity = Number(data.quantity);
  try {
    const r = await api("/admin/stock/movements", { method: "POST", body: JSON.stringify(data) });
    status($("#movement-status"), `${TYPES[r.movement.type]} enregistré : stock ${r.movement.on_hand_after}.`);
    refreshSoon();
  } catch (err) {
    status($("#movement-status"), err.message, false);
  }
});

// Image : formulaire présigné (S3 impose la clé, le type et la taille), puis l'API
// rattache l'image au produit ; la Lambda thumbnailer produit les miniatures.
$("#image-upload").addEventListener("click", async () => {
  const file = $("#image-file").files[0];
  const pid = $("#image-product").value;
  if (!file) return status($("#image-status"), "Choisissez une image.", false);
  try {
    const form = await api(`/admin/products/${pid}/image-upload`, { method: "POST", body: JSON.stringify({ content_type: file.type }) });
    if (file.size > form.max_bytes) throw new Error("image trop lourde (5 Mo au plus)");
    const data = new FormData();
    Object.entries(form.fields).forEach(([k, v]) => data.append(k, v));
    data.append("file", file); // le fichier doit être le dernier champ
    const post = await fetch(form.url, { method: "POST", body: data });
    if (!post.ok) throw new Error(`dépôt dans S3 refusé (${post.status})`);
    await api(`/admin/products/${pid}/image`, { method: "POST", body: JSON.stringify({ image_id: form.image_id }) });
    status($("#image-status"), `Image de ${pid} enregistrée ; miniatures en cours de création.`);
    refreshSoon();
  } catch (err) {
    status($("#image-status"), err.message, false);
  }
});

$("#export").addEventListener("click", async () => {
  try {
    const r = await api("/admin/catalog/export", { method: "POST" });
    status($("#csv-status"), `Export prêt (lien valable ${r.expires_in} s).`);
    window.location.href = r.url;
  } catch (err) {
    status($("#csv-status"), err.message, false);
  }
});

$("#import").addEventListener("click", async () => {
  const file = $("#import-file").files[0];
  if (!file) return status($("#csv-status"), "Choisissez un fichier CSV.", false);
  try {
    // Le fichier va directement dans S3 par un lien présigné, puis l'API l'importe.
    const target = await api("/admin/catalog/import-url", { method: "POST" });
    const put = await fetch(target.url, { method: "PUT", headers: { "Content-Type": target.content_type }, body: file });
    if (!put.ok) throw new Error(`dépôt dans S3 refusé (${put.status})`);
    const r = await api("/admin/catalog/import", { method: "POST", body: JSON.stringify({ key: target.key }) });
    status($("#csv-status"), `Import : ${r.created} créé(s), ${r.updated} modifié(s), ${r.movements} mouvement(s) d'inventaire.`);
    refreshSoon();
  } catch (err) {
    status($("#csv-status"), err.message, false);
  }
});

fetch("/api/health").then((r) => r.json()).then((h) => { $("#backend").textContent = `${h.backend} · ${h.region}`; }).catch(() => {});
Auth.init().then(() => {
  if (Auth.user()) refresh().then(() => { $("#login").hidden = true; $("#app").hidden = false; }).catch(() => {});
});
