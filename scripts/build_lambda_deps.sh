#!/usr/bin/env bash
# Prépare le dossier de la Lambda "thumbnailer" : handler + Pillow pour la plateforme
# Lambda (Linux arm64, CPython 3.12), jamais les binaires de la machine qui construit.
#
#   ./scripts/build_lambda_deps.sh        (appelé par up.sh avant terraform apply)
#
# Pillow est installé depuis la wheel manylinux aarch64 figée dans
# scripts/lambda-requirements.txt (pip --require-hashes) ; le script échoue si un
# binaire n'est pas un ELF aarch64 (par exemple un Mach-O de macOS).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/infra/.build/thumbnailer"

rm -rf "$OUT"
mkdir -p "$OUT"
python3 -m pip install --quiet --disable-pip-version-check --target "$OUT" \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --no-deps --require-hashes -r "$ROOT/scripts/lambda-requirements.txt"

bad=0
count=0
while IFS= read -r so; do
  count=$((count + 1))
  case "$(file -b "$so")" in
    ELF\ 64-bit*aarch64*) ;;
    *) echo "❌ binaire hors plateforme Lambda : $so ($(file -b "$so" | cut -c1-60))"; bad=1 ;;
  esac
done < <(find "$OUT" -name '*.so*' -type f)
if [ "$bad" -ne 0 ] || [ "$count" -eq 0 ]; then
  echo "❌ dépendances de la Lambda thumbnailer invalides"
  exit 1
fi

cp "$ROOT/src/lambda/thumbnailer/handler.py" "$OUT/handler.py"
version=$(basename "$(find "$OUT" -maxdepth 1 -name 'pillow-*.dist-info' | head -1)" .dist-info)
echo "thumbnailer : ${version#pillow-} (Pillow), $count binaires ELF aarch64 vérifiés"
