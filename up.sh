#!/usr/bin/env bash
# Construit (ou met à jour) toute l'infrastructure : ./up.sh
set -euo pipefail
cd "$(dirname "$0")"
# Lambda "thumbnailer" : Pillow pour la plateforme Lambda (Linux arm64), vérifié.
./scripts/build_lambda_deps.sh
cd infra
terraform init -input=false
# Identité SES vérifiée une fois hors Terraform : arrêt clair si elle manque.
../scripts/check_ses.sh
terraform apply -input=false -auto-approve
terraform output
# Compte admin de démonstration (demo_admin_email), mot de passe affiché en dernier.
../scripts/create_demo_admin.sh
