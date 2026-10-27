#!/usr/bin/env bash
# Construit (ou met à jour) toute l'infrastructure : ./up.sh
set -euo pipefail
cd "$(dirname "$0")/infra"
terraform init -input=false
terraform apply -input=false -auto-approve
terraform output
