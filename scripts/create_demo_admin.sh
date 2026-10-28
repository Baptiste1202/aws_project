#!/usr/bin/env bash
# Crée le compte admin de démonstration si demo_admin_email est renseignée dans
# infra/terraform.tfvars. Appelé par up.sh après terraform apply.
#
# Le mot de passe est tiré au hasard à chaque appel et n'existe que dans ce shell :
# il est affiché une seule fois, jamais écrit dans un fichier ni dans l'état Terraform
# (l'utilisateur est créé par l'AWS CLI, pas par Terraform). Si le compte existe déjà,
# un nouveau mot de passe le remplace. down.sh supprime le pool, donc le compte.
#
#   ./scripts/create_demo_admin.sh     # code 0 : compte prêt (ou variable vide), 1 sinon
set -euo pipefail
cd "$(dirname "$0")/../infra"
export AWS_PAGER=""

value() { echo "$1" | terraform console 2>/dev/null | tr -d '"'; }
EMAIL="$(value var.demo_admin_email)"
REGION="$(value var.region)"

if [ -z "$EMAIL" ]; then
  echo "Compte admin de démonstration : demo_admin_email vide, aucun compte créé."
  exit 0
fi

POOL="$(terraform output -raw cognito_user_pool_id)"
# Politique du pool : 8 caractères minimum, minuscule, majuscule et chiffre.
PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(15) + "Aa1")')"

idp() { aws cognito-idp "$@" --region "$REGION" --user-pool-id "$POOL" >/dev/null; }

if aws cognito-idp admin-get-user --region "$REGION" --user-pool-id "$POOL" \
  --username "$EMAIL" >/dev/null 2>&1; then
  echo "Compte admin de démonstration : $EMAIL existe déjà, nouveau mot de passe."
else
  # SUPPRESS : pas d'e-mail d'invitation (le mot de passe ne part pas par e-mail).
  idp admin-create-user --username "$EMAIL" --message-action SUPPRESS \
    --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true
fi
# --password=... : un mot de passe aléatoire qui commence par « - » serait lu comme
# une option. --permanent : pas de changement imposé à la première connexion.
idp admin-set-user-password --username "$EMAIL" "--password=$PASSWORD" --permanent
idp admin-add-user-to-group --username "$EMAIL" --group-name admin

echo
echo "Compte admin de démonstration (back-office : /admin.html)"
echo "  identifiant  : $EMAIL"
echo "  mot de passe : $PASSWORD"
echo "  Affiché une seule fois, enregistré nulle part : relancer ./up.sh en crée un nouveau."
