#!/usr/bin/env bash
# Supprime TOUTE l'infrastructure (plus aucun coût) : ./down.sh
set -euo pipefail
cd "$(dirname "$0")/infra"
terraform init -input=false
terraform destroy -input=false -auto-approve
