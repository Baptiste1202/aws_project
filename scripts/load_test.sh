#!/usr/bin/env bash
# Test de charge k6 des trois solutions, l'une après l'autre (elles ne se partagent pas
# le quota Lambda pendant la mesure), puis résumé comparatif.
#
#   ./scripts/load_test.sh              # local : http://localhost:8080, 8081, 8082
#   ./scripts/load_test.sh aws          # AWS : URLs lues dans terraform output
#
# k6 tourne dans l'image Docker grafana/k6 (rien à installer). Variables : VUS (8 au
# plus, quota de 10 exécutions Lambda simultanées du compte), DURATION (60s), OUT_DIR.
# Résumé : $OUT_DIR/k6_<solution>.json et $OUT_DIR/k6_resume.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-local}"
VUS="${VUS:-8}"
DURATION="${DURATION:-60s}"
OUT_DIR="${OUT_DIR:-$ROOT/docs/captures}"
K6_IMAGE="grafana/k6:0.54.0"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"  # chemin absolu : exigé par docker run -v

if [ "$MODE" = aws ]; then
  urls=("serverless=$(terraform -chdir="$ROOT/infra" output -raw serverless_url)"
        "vm=$(terraform -chdir="$ROOT/infra" output -raw vm_url)"
        "hybrid=$(terraform -chdir="$ROOT/infra" output -raw hybrid_url)")
else
  # Depuis le conteneur k6, la machine hôte s'appelle host.docker.internal.
  urls=("serverless=http://host.docker.internal:8081" "vm=http://host.docker.internal:8080"
        "hybrid=http://host.docker.internal:8082")
fi

for entry in "${urls[@]}"; do
  name="${entry%%=*}"
  url="${entry#*=}"
  echo "━━ k6 : $name ($url), $VUS utilisateurs virtuels, $DURATION"
  docker run --rm --add-host host.docker.internal:host-gateway \
    -v "$ROOT/scripts/load:/scripts:ro" -v "$OUT_DIR:/out" \
    -e BASE="$url" -e SOLUTION="$name" -e VUS="$VUS" -e DURATION="$DURATION" -e OUT="/out/k6_$name.json" \
    "$K6_IMAGE" run --quiet /scripts/k6_shop.js || true  # seuils indicatifs : on continue
done

python3 - "$OUT_DIR" "$MODE" <<'EOF'
import json, os, sys
out, mode = sys.argv[1], sys.argv[2]
rows = []
for name in ("serverless", "vm", "hybrid"):
    path = os.path.join(out, f"k6_{name}.json")
    if os.path.exists(path):
        rows.append(json.load(open(path)))
lines = [f"Test de charge k6 ({mode}) : {rows[0]['vus']} utilisateurs virtuels, {rows[0]['duration']} "
         "à charge pleine (+ 10 s de montée), parcours de lecture.", "",
         "| Solution | Requêtes | Req/s | Médiane | p95 | Max | Réponses attendues | 429 (quota Lambda) | 5xx |",
         "|---|---|---|---|---|---|---|---|---|"]
for r in rows:
    d = r["duration_ms"]
    lines.append(f"| {r['solution']} | {r['requests']} | {r['rps']:.1f} | {d['med']:.0f} ms | {d['p(95)']:.0f} ms | "
                 f"{d['max']:.0f} ms | {100 * r['ok_rate']:.1f} % | {r['throttled_429']} | {r['errors_5xx']} |")
lines += ["", "p95 par page (ms) :", "",
          "| Solution | " + " | ".join(rows[0]["pages"]) + " |", "|---|" + "---|" * len(rows[0]["pages"])]
for r in rows:
    lines.append(f"| {r['solution']} | " + " | ".join(f"{v['p(95)']:.0f}" for v in r["pages"].values()) + " |")
text = "\n".join(lines) + "\n"
open(os.path.join(out, "k6_resume.md"), "w").write(text)
print("\n" + text)
EOF
