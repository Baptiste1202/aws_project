#!/usr/bin/env bash
# Scénarios de test de la boutique (§4.3 du sujet).
#
#   ./scripts/test_scenario.sh                         # local : http://localhost:8080 (VM)
#   ./scripts/test_scenario.sh http://localhost:8081   # local : serverless (8081) ou hybride (8082)
#   ./scripts/test_scenario.sh https://xxxx.cloudfront.net   # AWS (serverless_url, vm_url ou hybrid_url)
#
# En local, le scénario 5 lit le bucket d'audit dans LocalStack (http://localhost:4566)
# s'il répond ; il est ignoré sinon.
#
# Solution hybride : le stock affiché (DynamoDB) rattrape RDS de façon asynchrone,
# le script relit donc le stock jusqu'à la valeur attendue (20 s au plus), et le
# suivi d'une commande peut répondre 404 pendant quelques instants après sa création.
#
# Prérequis : curl, python3 (lecture du JSON). En mode AWS : aws CLI, et le nom du
# bucket d'audit, lu dans `terraform output` ou dans la variable AUDIT_BUCKET.
# Variable optionnelle : TIMEOUT (secondes d'attente du statut SHIPPED, 90 par défaut).
#
# Attention : le scénario 4 vide le stock d'un produit. Le stock est remis à zéro
# par `docker compose ... down` puis `up` en local, et par `./up.sh` sur AWS.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:-http://localhost:8080}"
BASE="${BASE%/}"
TIMEOUT="${TIMEOUT:-90}"
case "$BASE" in
  *localhost* | *127.0.0.1*) MODE=local ;;
  *) MODE=aws ;;
esac
LOCALSTACK="${LOCALSTACK_URL:-http://localhost:4566}"

# s3cli ... : AWS CLI vers le vrai AWS, ou vers LocalStack en local (identifiants factices).
s3cli() {
  if [ "$MODE" = local ]; then
    AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=eu-west-3 \
      aws --endpoint-url "$LOCALSTACK" "$@"
  else
    aws "$@"
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

RESULTS=()
NOTES=()
FAILED=0
ORDER_ID=""
ORDER_DATE=""

# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

title() { printf '\n\033[1m━━ %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
ok() { printf '   ✅ %s\n' "$2"; RESULTS+=("✅ $1"); }
ko() { printf '   ❌ %s\n' "$2"; RESULTS+=("❌ $1 : $2"); FAILED=$((FAILED + 1)); }
skip() { printf '   ⏭️  %s\n' "$2"; RESULTS+=("⏭️  $1 (ignoré : $2)"); }

# api MÉTHODE CHEMIN [CORPS] [FICHIER] : écrit la réponse dans FICHIER, affiche le code HTTP.
api() {
  local out="${4:-$TMP/body}"
  if [ -n "${3:-}" ]; then
    curl -sS -m 20 -o "$out" -w '%{http_code}' -X "$1" "$BASE/api$2" \
      -H 'Content-Type: application/json' -d "$3" 2>"$TMP/curl.err"
  else
    curl -sS -m 20 -o "$out" -w '%{http_code}' -X "$1" "$BASE/api$2" 2>"$TMP/curl.err"
  fi
}

# json FICHIER EXPRESSION : évalue une expression Python sur le JSON (variable d).
json() {
  python3 -c "import json, sys
d = json.load(open(sys.argv[1]))
print($2)" "$1" 2>/dev/null
}

# missing_events "LISTE" : événements du parcours d'une commande absents de LISTE.
missing_events() {
  local e
  for e in ORDER_CREATED ORDER_PAID ORDER_PACKED ORDER_SHIPPED ORDER_IN_TRANSIT ORDER_DELIVERED; do
    case " $1 " in
      *" $e "*) ;;
      *) printf '%s ' "$e" ;;
    esac
  done
}

stock_of() {
  [ "$(api GET "/products/$1" "" "$TMP/stock")" = 200 ] && json "$TMP/stock" 'd["stock"]'
}

# wait_stock PRODUIT ATTENDU : relit le stock jusqu'à la valeur attendue, 20 s au plus
# (cohérence à terme de la solution hybride ; immédiat pour les deux autres).
# Affiche la dernière valeur lue, que le test compare ensuite.
wait_stock() {
  local deadline=$((SECONDS + 20)) s
  while :; do
    s=$(stock_of "$1")
    if [ "$s" = "$2" ] || [ "$SECONDS" -ge "$deadline" ]; then
      echo "$s"
      return
    fi
    sleep 1
  done
}

# ---------------------------------------------------------------------------

title "Boutique : $BASE (mode $MODE)"
code=$(api GET /health)
if [ "$code" != 200 ]; then
  info "❌ /api/health répond $code : la boutique n'est pas joignable."
  [ -s "$TMP/curl.err" ] && info "$(cat "$TMP/curl.err")"
  [ "$MODE" = local ] && info "Démarrer la stack : docker compose -f local/docker-compose.yml up --build -d"
  exit 1
fi
info "Backend : $(json "$TMP/body" 'd["backend"]') ($(json "$TMP/body" 'd["region"]')), paiement $(json "$TMP/body" 'd["payment"]["type"]'), traitement $(json "$TMP/body" 'd["fulfillment"]')"
cp "$TMP/body" "$TMP/health"

# ---------------------------------------------------------------------------
title "1. Afficher le catalogue"
# ---------------------------------------------------------------------------

code=$(api GET /products "" "$TMP/catalog")
count=$(json "$TMP/catalog" 'len(d["products"])')
if [ "$code" = 200 ] && [ "${count:-0}" -gt 0 ]; then
  json "$TMP/catalog" '"\n".join("   %-7s %-8s %-28s %7s €  stock %3s" % (p["id"], p["category"], p["name"], p["price"], p["stock"]) for p in d["products"])'
  ok "Catalogue" "$count produits renvoyés par GET /api/products"
else
  ko "Catalogue" "GET /api/products a répondu $code avec ${count:-0} produit"
fi

# ---------------------------------------------------------------------------
title "2. Commande de plusieurs produits, payée, suivie jusqu'à DELIVERED"
# ---------------------------------------------------------------------------

# Les deux produits les mieux approvisionnés : 2 du premier, 1 du second.
read -r P1 S1 STOCK1 P2 S2 STOCK2 <<<"$(json "$TMP/catalog" \
  '" ".join("%s %s %s" % (p["id"], p["sizes"][0], p["stock"]) for p in sorted(d["products"], key=lambda p: -p["stock"])[:2])')"

if [ -z "${P2:-}" ] || [ "${STOCK1:-0}" -lt 2 ] || [ "${STOCK2:-0}" -lt 1 ]; then
  skip "Commande payée jusqu'à DELIVERED" "stock insuffisant, réinitialiser les données"
else
  info "Commande : 2 × $P1 (taille $S1) + 1 × $P2 (taille $S2)"
  body="{\"customer\":{\"name\":\"Scénario de test\",\"email\":\"test@example.com\"},\"items\":[
    {\"product_id\":\"$P1\",\"size\":\"$S1\",\"quantity\":2},
    {\"product_id\":\"$P2\",\"size\":\"$S2\",\"quantity\":1}]}"
  code=$(api POST /orders "$body" "$TMP/order")
  ORDER_ID=$(json "$TMP/order" 'd["order_id"]')
  if [ "$code" != 201 ] || [ -z "$ORDER_ID" ]; then
    ko "Commande payée jusqu'à DELIVERED" "POST /api/orders a répondu $code : $(cat "$TMP/order")"
  else
    ORDER_DATE=$(json "$TMP/order" 'd["created_at"][:10]')
    info "$ORDER_ID créée : $(json "$TMP/order" 'd["status"]'), total $(json "$TMP/order" 'd["total"]') € (dont livraison $(json "$TMP/order" 'd["shipping_fee"]') €)"
    # Paiement par carte de test, comme Stripe.js (scripts/testpay.py), puis confirm-payment.
    if python3 "$ROOT/scripts/testpay.py" "$BASE" "$TMP/order" >"$TMP/paid"; then
      cp "$TMP/paid" "$TMP/order"
      info "Payée ($(json "$TMP/order" 'd["payment"]["provider"]')) : $(json "$TMP/order" 'd["status"]')"
    else
      info "❌ paiement refusé : $(cat "$TMP/paid")"
    fi
    start=$SECONDS
    last=$(json "$TMP/order" 'd["status"]')
    status=$last
    while [ "$status" != DELIVERED ] && [ "$status" != CANCELLED ] && [ $((SECONDS - start)) -lt "$TIMEOUT" ]; do
      sleep 2
      # 404 possible juste après la création (hybride : commande pas encore projetée) : on réessaie.
      [ "$(api GET "/orders/$ORDER_ID" "" "$TMP/order")" = 200 ] || continue
      status=$(json "$TMP/order" 'd["status"]')
      if [ "$status" != "$last" ]; then
        info "+$((SECONDS - start)) s : $status"
        last=$status
      fi
    done
    history=$(json "$TMP/order" '",".join(h["status"] for h in d["history"])')
    tracking=$(json "$TMP/order" 'd["shipping"]["carrier"] + " " + d["shipping"]["tracking_number"]')
    new1=$(wait_stock "$P1" $((STOCK1 - 2)))
    new2=$(wait_stock "$P2" $((STOCK2 - 1)))
    if [ "$status" != DELIVERED ]; then
      ko "Commande payée jusqu'à DELIVERED" "toujours $status après $TIMEOUT s"
    elif [ "$history" != "PENDING_PAYMENT,CONFIRMED,PACKED,SHIPPED,IN_TRANSIT,DELIVERED" ]; then
      ko "Commande payée jusqu'à DELIVERED" "historique inattendu : $history"
    elif [ "$new1" -gt $((STOCK1 - 2)) ] || [ "$new2" -gt $((STOCK2 - 1)) ]; then
      ko "Commande payée jusqu'à DELIVERED" "stock non décrémenté ($P1 : $STOCK1 → $new1, $P2 : $STOCK2 → $new2)"
    else
      info "Expédiée : $tracking"
      info "Stock : $P1 $STOCK1 → $new1, $P2 $STOCK2 → $new2"
      ok "Commande payée jusqu'à DELIVERED" "payée → PACKED → SHIPPED → IN_TRANSIT → DELIVERED en $((SECONDS - start)) s ($(json "$TMP/health" 'd.get("fulfillment") or "?"')), stock décrémenté"
    fi
  fi
fi

# ---------------------------------------------------------------------------
title "3. Commande refusée pour stock insuffisant (409)"
# ---------------------------------------------------------------------------

# Produit au plus petit stock non nul : on en demande stock + 1 (lignes de 10 au plus).
api GET /products "" "$TMP/catalog" >/dev/null
read -r P S STOCK <<<"$(json "$TMP/catalog" \
  '" ".join("%s %s %s" % (p["id"], p["sizes"][0], p["stock"]) for p in [p for p in sorted(d["products"], key=lambda p: p["stock"]) if 0 < p["stock"] < 200][:1])')"

if [ -z "${P:-}" ]; then
  skip "Stock insuffisant" "aucun produit avec un stock entre 1 et 199"
else
  want=$((STOCK + 1))
  lines=$(python3 -c "import json, sys
q, p, s = int(sys.argv[1]), sys.argv[2], sys.argv[3]
print(json.dumps([{'product_id': p, 'size': s, 'quantity': min(10, q - i)} for i in range(0, q, 10)]))" "$want" "$P" "$S")
  info "Demande de $want × $P alors que le stock est de $STOCK"
  code=$(api POST /orders "{\"customer\":{\"name\":\"Scénario de test\",\"email\":\"test@example.com\"},\"items\":$lines}")
  after=$(stock_of "$P")
  if [ "$code" = 409 ] && [ "$after" = "$STOCK" ]; then
    info "Réponse : $(json "$TMP/body" 'd["error"]')"
    ok "Stock insuffisant" "409 renvoyé, stock de $P inchangé ($after)"
  else
    ko "Stock insuffisant" "attendu 409 et stock $STOCK, obtenu $code et stock $after"
  fi
fi

# ---------------------------------------------------------------------------
title "4. Commandes simultanées : le stock ne devient jamais négatif"
# ---------------------------------------------------------------------------

# Toutes les commandes partent en même temps et demandent plus que le stock total.
api GET /products "" "$TMP/catalog" >/dev/null
read -r P S STOCK <<<"$(json "$TMP/catalog" \
  '" ".join("%s %s %s" % (p["id"], p["sizes"][0], p["stock"]) for p in [p for p in sorted(d["products"], key=lambda p: p["stock"]) if p["stock"] > 0][:1])')"

if [ -z "${P:-}" ]; then
  skip "Commandes simultanées" "plus aucun produit en stock"
else
  q=$(((STOCK + 19) / 20))
  [ "$q" -gt 10 ] && q=10
  n=$((STOCK / q + 5))
  info "$n commandes simultanées de $q × $P (stock $STOCK, demande totale $((n * q)))"
  body="{\"customer\":{\"name\":\"Scénario de test\",\"email\":\"test@example.com\"},\"items\":[{\"product_id\":\"$P\",\"size\":\"$S\",\"quantity\":$q}]}"
  for i in $(seq 1 "$n"); do
    curl -sS -m 30 -o "$TMP/resp.$i" -w '%{http_code}\n' -X POST "$BASE/api/orders" \
      -H 'Content-Type: application/json' -d "$body" >"$TMP/code.$i" 2>/dev/null &
  done
  wait
  created=$(cat "$TMP"/code.* | grep -c '^201$')
  refused=$(cat "$TMP"/code.* | grep -c '^409$')
  # 429 : Lambda a refusé la requête avant de l'exécuter (quota de concurrence du
  # compte atteint). Ce n'est pas une erreur de la boutique : comptée à part.
  # LocalStack signale ce même refus par un 500 dont le corps est de type
  # TooManyRequestsException : compté aussi (une autre erreur 500 reste un échec).
  limited=0
  for i in $(seq 1 "$n"); do
    case "$(cat "$TMP/code.$i")" in
      429) limited=$((limited + 1)) ;;
      500) grep -q '"TooManyRequestsException"' "$TMP/resp.$i" 2>/dev/null && limited=$((limited + 1)) ;;
    esac
  done
  others=$((n - created - refused - limited))
  expected=$((STOCK - created * q))
  after=$(wait_stock "$P" "$expected")
  info "Acceptées (201) : $created, refusées (409) : $refused, limitées par Lambda : $limited, autres : $others"
  info "Stock final : $after (attendu $STOCK - $created × $q = $expected)"
  if [ "$limited" -gt 0 ]; then
    info "⚠️  $limited requête(s) limitée(s) par Lambda : quota de concurrence du compte atteint,"
    info "    requêtes rejetées avant traitement, sans effet sur le stock."
    NOTES+=("⚠️  Commandes simultanées : $limited requête(s) sur $n limitée(s) par Lambda, comptées à part")
  fi
  if [ "$others" -ne 0 ]; then
    ko "Commandes simultanées" "$others réponses ni 201, ni 409, ni 429 ($(sort "$TMP"/code.* | uniq -c | tr -s ' \n' ' '))"
  elif [ -z "$after" ] || [ "$after" -lt 0 ] || [ "$after" -ne "$expected" ]; then
    ko "Commandes simultanées" "stock incohérent : $after"
  elif [ "$limited" -gt 0 ]; then
    ok "Commandes simultanées" "$created/$n acceptées ($limited limitées par Lambda), stock final $after ≥ 0 et cohérent"
  else
    ok "Commandes simultanées" "$created/$n acceptées, stock final $after ≥ 0 et cohérent"
  fi
fi

# ---------------------------------------------------------------------------
title "5. Fichiers d'audit dans S3"
# ---------------------------------------------------------------------------

if [ "$MODE" = local ] && ! curl -sf -m 3 "$LOCALSTACK/_localstack/health" >/dev/null 2>&1; then
  skip "Audit S3" "mode local sans LocalStack ($LOCALSTACK ne répond pas)"
elif [ -z "$ORDER_ID" ]; then
  skip "Audit S3" "pas de commande créée au scénario 2"
else
  if [ "$MODE" = local ]; then
    BUCKET="${AUDIT_BUCKET:-threadly-local-audit}"
  else
    BUCKET="${AUDIT_BUCKET:-$(terraform -chdir="$ROOT/infra" output -raw audit_bucket 2>/dev/null)}"
  fi
  if [ -z "$BUCKET" ]; then
    ko "Audit S3" "bucket inconnu : lancer depuis le dépôt après ./up.sh, ou définir AUDIT_BUCKET"
  else
    # La Lambda audit archive chaque événement : on attend les 6 de la commande du scénario 2.
    today=$(date -u +%F)
    start=$SECONDS
    found=""
    while [ $((SECONDS - start)) -lt 60 ]; do
      rm -rf "$TMP/audit"
      s3cli s3 cp "s3://$BUCKET/events/" "$TMP/audit" --recursive --quiet \
        --exclude '*' --include "*date=$ORDER_DATE/*" --include "*date=$today/*" 2>"$TMP/aws.err"
      found=$(grep -rl "$ORDER_ID" "$TMP/audit" 2>/dev/null | sed 's|.*event_type=\([A-Z_]*\)/.*|\1|' | sort -u | tr '\n' ' ')
      [ -z "$(missing_events "$found")" ] && break
      sleep 5
    done
    info "Derniers fichiers de s3://$BUCKET/events/ :"
    s3cli s3 ls "s3://$BUCKET/events/" --recursive 2>/dev/null | sort | tail -6 | sed 's/^/     /'
    if [ -s "$TMP/aws.err" ] && [ -z "$found" ]; then
      ko "Audit S3" "$(head -1 "$TMP/aws.err")"
    elif [ -z "$(missing_events "$found")" ]; then
      ok "Audit S3" "les 6 événements de $ORDER_ID sont archivés ($found)"
    else
      ko "Audit S3" "événements archivés pour $ORDER_ID : ${found:-aucun} (manquants : $(missing_events "$found"))"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Scénarios 6 à 14 (lot 1 : stock), en Python : voir scripts/scenarios_stock.py.
# Ignorés avec SKIP_STOCK=1 (ils créent des produits de test ZZ-nnnnnnnnn, uniques à chaque lancement).
# ---------------------------------------------------------------------------

run_python_scenarios() {
  while IFS= read -r line; do
    case "$line" in
      RESULT:❌*) RESULTS+=("${line#RESULT:}"); FAILED=$((FAILED + 1)) ;;
      RESULT:*) RESULTS+=("${line#RESULT:}") ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < <(LOCALSTACK_URL="$LOCALSTACK" python3 "$ROOT/scripts/$1" "$BASE" "$MODE" 2>&1)
}

[ "${SKIP_STOCK:-0}" != 1 ] && run_python_scenarios scenarios_stock.py
# Scénarios 15 à 20 (lot 2a : comptes, recherche, codes promo, avis, souhaits).
[ "${SKIP_CLIENT:-0}" != 1 ] && run_python_scenarios scenarios_client.py
# Scénarios 21 et 22 (lot 2b : images produit, suivi en temps réel).
[ "${SKIP_MEDIA:-0}" != 1 ] && run_python_scenarios scenarios_media.py
# Scénarios 23 à 28 (lot 3a : paiement, purge des impayés, livraison, facture, e-mails).
[ "${SKIP_ORDER:-0}" != 1 ] && run_python_scenarios scenarios_order.py
# Scénarios 29 à 31 (lot 3b : retours et remboursements, réappro fournisseur).
[ "${SKIP_RETURNS:-0}" != 1 ] && run_python_scenarios scenarios_returns.py
# Scénarios 32 à 35 (lot 4 : tableau de bord, rapport quotidien, Athena + Glue, X-Ray).
[ "${SKIP_PILOTAGE:-0}" != 1 ] && run_python_scenarios scenarios_pilotage.py

# ---------------------------------------------------------------------------
title "Résumé"
# ---------------------------------------------------------------------------

for r in "${RESULTS[@]}"; do info "$r"; done
# ${NOTES[@]+...} : tableau vide sans erreur avec set -u sous bash 3.2 (macOS).
for r in ${NOTES[@]+"${NOTES[@]}"}; do info "$r"; done
echo
if [ "$FAILED" -eq 0 ]; then
  info "Tous les scénarios exécutés sont passés."
else
  info "$FAILED scénario(s) en échec."
fi
exit $((FAILED > 0))
