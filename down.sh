#!/usr/bin/env bash
# Supprime TOUTE l'infrastructure (plus aucun coût) : ./down.sh
set -euo pipefail
cd "$(dirname "$0")"
# Terraform évalue aussi les archives des Lambdas pendant un destroy.
./scripts/build_lambda_deps.sh
cd infra
terraform init -input=false
terraform destroy -input=false -auto-approve
