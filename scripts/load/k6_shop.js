// Test de charge k6 d'une solution Threadly (lot 4) : parcours de lecture d'un visiteur.
//
//   k6 run -e BASE=https://xxxx.cloudfront.net -e SOLUTION=serverless scripts/load/k6_shop.js
//   (ou ./scripts/load_test.sh, qui le lance par l'image Docker grafana/k6 sur les trois)
//
// Variables : BASE (URL de la boutique), SOLUTION (nom affiché), VUS (8 par défaut et au
// plus : le compte est limité à 10 exécutions Lambda simultanées, partagées par toutes
// les Lambdas), DURATION (60s), OUT (fichier JSON du résumé).
// Le parcours ne fait que des lectures (catalogue, recherche, fiche, avis, suivi
// d'une commande inexistante) : aucune commande ni aucun paiement Stripe n'est créé.
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate } from "k6/metrics";

const BASE = (__ENV.BASE || "http://localhost:8080").replace(/\/$/, "");
const SOLUTION = __ENV.SOLUTION || BASE;
const VUS = Math.min(parseInt(__ENV.VUS || "8", 10), 8);
const DURATION = __ENV.DURATION || "60s";

const throttled = new Counter("throttled_429");   // refus de la plateforme (quota Lambda)
const serverErrors = new Counter("errors_5xx");
const ok = new Rate("ok_responses");

export const options = {
  scenarios: {
    visiteurs: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "10s", target: VUS },
        { duration: DURATION, target: VUS },
        { duration: "5s", target: 0 },
      ],
      gracefulRampDown: "5s",
    },
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "max"],
  // Indicatifs (le test ne s'arrête pas) : la comparaison est dans le résumé.
  // Un seuil par page : k6 ne calcule les statistiques d'un tag que s'il a un seuil.
  thresholds: {
    "http_req_duration{page:catalogue}": ["p(95)<1500"],
    "http_req_duration{page:recherche}": ["p(95)<1500"],
    "http_req_duration{page:fiche}": ["p(95)<1500"],
    "http_req_duration{page:avis}": ["p(95)<1500"],
    "http_req_duration{page:suivi}": ["p(95)<1500"],
    "http_req_duration{page:sante}": ["p(95)<1500"],
    ok_responses: ["rate>0.95"],
  },
};

const PRODUCTS = ["TS-001", "TS-002", "TS-003", "PU-001", "PU-002", "BK-001", "BK-002"];
const SEARCHES = ["noir", "laine", "basket", "oversize", "lin"];

function get(path, page, expected = [200]) {
  const res = http.get(`${BASE}${path}`, { tags: { page }, timeout: "20s" });
  if (res.status === 429) throttled.add(1);
  if (res.status >= 500) serverErrors.add(1);
  const good = expected.includes(res.status);
  ok.add(good);
  check(res, { [`${page} : ${expected.join("/")}`]: () => good });
  return res;
}

export default function () {
  get("/api/products", "catalogue");
  sleep(0.5);
  const q = SEARCHES[Math.floor(Math.random() * SEARCHES.length)];
  get(`/api/products?q=${q}&sort=price`, "recherche");
  sleep(0.5);
  const pid = PRODUCTS[Math.floor(Math.random() * PRODUCTS.length)];
  get(`/api/products/${pid}`, "fiche");
  get(`/api/products/${pid}/reviews`, "avis");
  sleep(0.5);
  get("/api/orders/ORD-0000000000", "suivi", [404]);
  get("/api/health", "sante");
  sleep(1);
}

function metric(data, name, stat) {
  const m = data.metrics[name];
  return m ? m.values[stat] : null;
}

export function handleSummary(data) {
  const pages = {};
  for (const page of ["catalogue", "recherche", "fiche", "avis", "suivi", "sante"]) {
    const key = `http_req_duration{page:${page}}`;
    if (data.metrics[key]) pages[page] = data.metrics[key].values;
  }
  const summary = {
    solution: SOLUTION, base: BASE, vus: VUS, duration: DURATION,
    requests: metric(data, "http_reqs", "count"), rps: metric(data, "http_reqs", "rate"),
    duration_ms: data.metrics.http_req_duration.values,
    ok_rate: metric(data, "ok_responses", "rate"),
    throttled_429: metric(data, "throttled_429", "count") || 0,
    errors_5xx: metric(data, "errors_5xx", "count") || 0,
    iterations: metric(data, "iterations", "count"), pages,
  };
  const out = { stdout: `\n${SOLUTION} : ${summary.requests} requêtes, ${summary.rps.toFixed(1)} req/s, ` +
    `p95 ${summary.duration_ms["p(95)"].toFixed(0)} ms, réponses attendues ${(100 * summary.ok_rate).toFixed(1)} %, ` +
    `429 : ${summary.throttled_429}, 5xx : ${summary.errors_5xx}\n` };
  if (__ENV.OUT) out[__ENV.OUT] = JSON.stringify(summary, null, 2);
  return out;
}
