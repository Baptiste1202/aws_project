# ---------------------------------------------------------------------------
# Lot 3a : paiement Stripe (mode test), e-mails SES, workflow des commandes.
#
#   Paiement : la clé secrète Stripe va dans SSM Parameter Store (SecureString),
#     jamais dans le code ni dans une variable d'environnement ; elle est lue à
#     l'exécution par l'API (Lambda api, EC2) et par la purge (sweeper, EC2).
#     payment_mode = "simulated" (choix explicite) : pas de Stripe, commande confirmée
#     aussitôt ; en mode "stripe" (par défaut), les clés sont obligatoires.
#   E-mails : identité SES de notification_email vérifiée une fois hors Terraform
#     (README) ; tant que le compte est en sandbox, tous les e-mails y vont.
#   Workflow : Step Functions (Standard, facturé à la transition, 4 000 gratuites par
#     mois) pour le serverless et l'hybride. La VM garde son worker SQS sur EC2.
#   Réappro (lot 3b) : machine restock pour le serverless ; worker EC2 pour la VM et
#     l'hybride (dont le stock est dans RDS).
# ---------------------------------------------------------------------------

locals {
  # Clés obligatoires en mode "stripe" (validation de payment_mode, variables.tf).
  stripe_enabled = var.payment_mode == "stripe"
  stripe_param   = "/${local.name}/stripe/secret-key"
  # Configuration publique (lue aussi par la Lambda de lecture de l'hybride).
  stripe_public_env = local.stripe_enabled ? { STRIPE_PUBLISHABLE_KEY = var.stripe_publishable_key } : {}
  # Services qui appellent Stripe : nom du paramètre SSM, pas la clé.
  stripe_env = local.stripe_enabled ? merge(local.stripe_public_env, { STRIPE_SECRET_PARAM = local.stripe_param }) : {}
  stripe_statements = local.stripe_enabled ? [{
    Effect   = "Allow"
    Action   = ["ssm:GetParameter"]
    Resource = [aws_ssm_parameter.stripe_secret[0].arn]
  }] : []

  mail_enabled = var.notification_email != ""
  # ARN construit : l'identité n'est pas gérée par Terraform (bloc check ci-dessous).
  ses_identity_arn = "arn:${data.aws_partition.current.partition}:ses:${var.region}:${local.account_id}:identity/${var.notification_email}"

  # ARN construits (et non lus sur les ressources) : les Lambdas qui démarrent les
  # workflows ne dépendent pas des machines d'état, qui dépendent de la Lambda fulfillment.
  state_machine_arns = {
    serverless = "arn:${data.aws_partition.current.partition}:states:${var.region}:${local.account_id}:stateMachine:${local.name}-fulfillment"
    hybrid     = "arn:${data.aws_partition.current.partition}:states:${var.region}:${local.account_id}:stateMachine:${local.name}-hybrid-fulfillment"
    restock    = "arn:${data.aws_partition.current.partition}:states:${var.region}:${local.account_id}:stateMachine:${local.name}-restock"
  }
  # Machine d'état => (définition, Lambda appelée par ses étapes). Réappro (lot 3b) :
  # serverless seulement ; la VM et l'hybride le font dans leur worker EC2.
  state_machines = merge(
    var.deploy_serverless ? {
      serverless = { name = "${local.name}-fulfillment", template = "fulfillment.asl.json", fn = "fulfillment" }
      restock    = { name = "${local.name}-restock", template = "restock.asl.json", fn = "supplier" }
    } : {},
    var.deploy_hybrid ? {
      hybrid = { name = "${local.name}-hybrid-fulfillment", template = "fulfillment.asl.json", fn = "hybrid-fulfillment" }
    } : {},
  )
}

resource "aws_ssm_parameter" "stripe_secret" {
  count       = local.stripe_enabled ? 1 : 0
  name        = local.stripe_param
  description = "Clé secrète Stripe (mode test)"
  type        = "SecureString"
  value       = var.stripe_secret_key
}

# Identité SES créée et vérifiée une seule fois, à la main (README, « Vérification SES ») :
# Terraform la lit sans la gérer, down.sh ne la supprime pas et up.sh ne renvoie pas
# d'e-mail de vérification. Un bloc check (avertissement) plutôt qu'une postcondition
# (erreur) : une identité supprimée ne doit pas bloquer terraform destroy. up.sh, lui,
# s'arrête avant l'apply si elle est absente ou non vérifiée (scripts/check_ses.sh).
check "ses_identity" {
  # Pas de count dans un bloc check : sans notification_email, la lecture échoue et
  # ne produit qu'un avertissement, l'assertion étant alors satisfaite.
  data "aws_sesv2_email_identity" "main" {
    email_identity = var.notification_email
  }

  assert {
    condition     = !local.mail_enabled || try(data.aws_sesv2_email_identity.main.verified_for_sending_status, false)
    error_message = "L'identité SES ${var.notification_email} est absente ou non vérifiée dans ${var.region} : les e-mails ne partiront pas. Voir le README, « Vérification SES » (étape unique)."
  }
}

resource "aws_iam_role" "sfn" {
  count = length(local.state_machines) > 0 ? 1 : 0
  name  = "${local.name}-states"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "sfn" {
  count = length(local.state_machines) > 0 ? 1 : 0
  name  = "invoke-fulfillment"
  role  = aws_iam_role.sfn[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [for k, m in local.state_machines : aws_lambda_function.main[m.fn].arn]
      }, {
      # X-Ray (lot 4) : trace de chaque exécution, reliée à celles des Lambdas appelées.
      Effect   = "Allow"
      Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
      Resource = ["*"]
    }]
  })
}

resource "aws_sfn_state_machine" "fulfillment" {
  for_each = local.state_machines
  name     = each.value.name
  role_arn = aws_iam_role.sfn[0].arn
  type     = "STANDARD"
  definition = templatefile("${path.module}/templates/${each.value.template}", {
    fulfillment_arn = aws_lambda_function.main[each.value.fn].arn
    supplier_arn    = aws_lambda_function.main[each.value.fn].arn
    delay           = var.fulfillment_delay_seconds
  })

  tracing_configuration {
    enabled = true
  }

  depends_on = [aws_iam_role_policy.sfn]
}
