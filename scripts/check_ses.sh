#!/usr/bin/env bash
# Vérifie, avant terraform apply, que l'identité SES de notification_email existe et est
# vérifiée dans la région du déploiement. Elle est créée une seule fois, à la main
# (README, « Vérification SES »), et n'est jamais supprimée par down.sh ni par
# scripts/cleanup_orphans.sh. Appelé par up.sh après terraform init.
#
#   ./scripts/check_ses.sh     # code 0 : vérifiée (ou pas d'e-mail configuré), 1 sinon
set -uo pipefail
cd "$(dirname "$0")/../infra"
export AWS_PAGER=""

value() { echo "$1" | terraform console 2>/dev/null | tr -d '"'; }
EMAIL="$(value var.notification_email)"
REGION="$(value var.region)"

if [ -z "$EMAIL" ]; then
  echo "SES : notification_email vide, aucun e-mail ne sera envoyé."
  exit 0
fi

status="$(aws sesv2 get-email-identity --region "$REGION" --email-identity "$EMAIL" \
  --query VerifiedForSendingStatus --output text 2>&1)"
case "$status" in
  True)
    echo "SES : identité $EMAIL vérifiée dans $REGION."
    exit 0
    ;;
  False)
    echo "❌ SES : l'identité $EMAIL existe dans $REGION mais n'est pas encore vérifiée." >&2
    echo "   Cliquer sur le lien de l'e-mail « Amazon Web Services – Email Address" >&2
    echo "   Verification Request » reçu à cette adresse, puis relancer ./up.sh." >&2
    ;;
  *NotFoundException*)
    echo "❌ SES : aucune identité $EMAIL dans $REGION. Étape unique (README, « Vérification SES ») :" >&2
    echo "   aws sesv2 create-email-identity --region $REGION --email-identity $EMAIL" >&2
    echo "   puis cliquer sur le lien reçu par e-mail et relancer ./up.sh." >&2
    echo "   Sans e-mail : notification_email = \"\" dans infra/terraform.tfvars." >&2
    ;;
  *)
    echo "❌ SES : lecture de l'identité $EMAIL impossible : $status" >&2
    ;;
esac
exit 1
