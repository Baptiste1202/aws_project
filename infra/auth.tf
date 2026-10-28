# ---------------------------------------------------------------------------
# Comptes clients et équipe : Amazon Cognito (offre Lite : gratuite jusqu'à 10 000
# utilisateurs actifs par mois, facturée à l'utilisateur actif, jamais à l'heure).
#   Groupes : admin (tout), magasinier (stock), client (tout utilisateur connecté).
# L'API vérifie elle-même les jetons (src/shop/auth.py) : signature RS256 avec les
# clés publiques du pool, émetteur, client, expiration. Seul ce pool est accepté.
# Les e-mails de confirmation passent par l'envoi intégré de Cognito (gratuit,
# limité à 50 par jour ; SES prendra le relais au lot 3).
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool" "main" {
  name                     = "${local.name}-users"
  user_pool_tier           = "LITE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "INACTIVE"
  mfa_configuration        = "OFF"

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Les comptes de l'équipe (admin, magasinier) sont créés par un administrateur ;
  # les clients s'inscrivent eux-mêmes depuis la boutique.
  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Threadly : votre code de confirmation"
    email_message        = "Votre code de confirmation Threadly : {####}"
  }
}

# Client public (front et script de test) : pas de secret, connexion par mot de passe
# (USER_PASSWORD_AUTH, toujours en HTTPS vers Cognito) ou SRP.
resource "aws_cognito_user_pool_client" "web" {
  name                          = "${local.name}-web"
  user_pool_id                  = aws_cognito_user_pool.main.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  id_token_validity             = 60
  access_token_validity         = 60
  refresh_token_validity        = 1

  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_group" "role" {
  for_each = {
    admin      = "Administration : catalogue, codes promo, import/export, stock"
    magasinier = "Stock : mouvements, journal, valorisation, stocks bas"
    client     = "Clients (tout utilisateur connecté a ce rôle, groupe facultatif)"
  }
  name         = each.key
  description  = each.value
  user_pool_id = aws_cognito_user_pool.main.id
}

locals {
  # Variables d'environnement de vérification des jetons (Lambdas et EC2).
  auth_env = {
    AUTH_ISSUER    = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
    AUTH_CLIENT_ID = aws_cognito_user_pool_client.web.id
  }
}
