# ---------------------------------------------------------------------------
# Fonctions Lambda (Python 3.12, uniquement la bibliothèque standard + boto3).
#   api       : API REST de la boutique (solution serverless)
#   processor : démarre le workflow Step Functions d'une commande payée (SQS, serverless)
#   fulfillment : étapes du workflow (emballage, expédition, transit, livraison)
#   supplier  : étapes du workflow de réappro (bon de commande, livraison fournisseur)
#   sweeper   : libère les réservations de panier expirées (solution serverless)
#   audit     : archivage S3 de tous les événements (commun à toutes les solutions)
#   thumbnailer : miniatures des images produit (S3 originals/ -> images/, Pillow)
#   realtime  : connexions WebSocket du suivi en temps réel (realtime.tf)
#   notifier  : pousse statuts de commande et retours en stock aux navigateurs
#   mailer    : e-mails par SES (confirmation et facture, expédition, livraison...)
#   reporter  : rapport quotidien CSV (EventBridge Scheduler, reporting.tf)
# Solution hybride (même code, autres tables, droits réduits) :
#   hybrid-read      : code "api" en lecture seule (catalogue, suivi, consultations d'admin)
#   hybrid-processor : code "processor" sur les tables hybrides (workflow hybride)
#   hybrid-fulfillment : code "fulfillment" sur la table hybride (statut : DynamoDB fait foi)
#   hybrid-projector : recopie dans DynamoDB produits, stock, mouvements et commandes venant de RDS
# "code" = dossier de src/lambda/ dont le handler est déployé.
# ---------------------------------------------------------------------------

locals {
  # Droits DynamoDB sur une table unique et son index gsi1.
  table_arns = { for k, t in aws_dynamodb_table.main : k => [t.arn, "${t.arn}/index/gsi1"] }
  # Bucket "data" : exports et imports CSV du catalogue, factures (liens présignés).
  data_statements = [
    {
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject"]
      Resource = [for prefix in ["exports", "imports", "invoices", "reports"] : "${aws_s3_bucket.data.arn}/${prefix}/*"]
    },
  ]
  # Étapes du workflow : transition conditionnelle dans DynamoDB, événement SNS.
  fulfillment_statements = { for k in ["shop", "hybrid"] : k => jsonencode([
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = local.table_arns[k] },
    { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
  ]) }
  sqs_read = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
  xray_statements = [{
    Effect   = "Allow"
    Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    Resource = ["*"] # X-Ray n'a pas d'ARN de ressource pour ces actions
  }]

  # env (map de chaînes) et statements (JSON) ont le même type pour toutes les
  # fonctions : nécessaire pour que le merge conditionnel soit bien typé.
  lambdas = merge(
    var.deploy_serverless ? {
      api = {
        code    = "api"
        timeout = 15
        memory  = 256
        env = tomap(merge(local.auth_env, local.stripe_env, {
          TABLE         = aws_dynamodb_table.main["shop"].name
          TOPIC_ARN     = aws_sns_topic.orders.arn
          ORIGIN_SECRET = random_password.origin_secret.result
          FULFILLMENT   = "step-functions"
          DATA_BUCKET   = aws_s3_bucket.data.id
          IMAGES_BUCKET = aws_s3_bucket.images.id
          IMAGES_URL    = "/images"
          REALTIME_URL  = local.realtime_url
        }))
        statements = jsonencode(concat([
          {
            Effect   = "Allow"
            Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"]
            Resource = local.table_arns["shop"]
          },
          { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
        ], local.data_statements, local.images_statements, local.stripe_statements))
      }
      processor = {
        code    = "processor"
        timeout = 30
        memory  = 128
        env = tomap({
          TABLE                     = aws_dynamodb_table.main["shop"].name
          STATE_MACHINE_ARN         = local.state_machine_arns["serverless"]
          RESTOCK_STATE_MACHINE_ARN = local.state_machine_arns["restock"]
        })
        statements = jsonencode([
          {
            Effect   = "Allow"
            Action   = ["states:StartExecution"]
            Resource = [local.state_machine_arns["serverless"], local.state_machine_arns["restock"]]
          },
          { Effect = "Allow", Action = local.sqs_read, Resource = [aws_sqs_queue.main["processing"].arn] },
        ])
      }
      fulfillment = {
        code       = "fulfillment"
        timeout    = 30
        memory     = 128
        env        = tomap({ TABLE = aws_dynamodb_table.main["shop"].name, TOPIC_ARN = aws_sns_topic.orders.arn })
        statements = local.fulfillment_statements["shop"]
      }
      supplier = {
        code    = "supplier"
        timeout = 30
        memory  = 128
        env     = tomap({ TABLE = aws_dynamodb_table.main["shop"].name, TOPIC_ARN = aws_sns_topic.orders.arn })
        statements = jsonencode([
          {
            Effect   = "Allow"
            Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
            Resource = local.table_arns["shop"]
          },
          { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
        ])
      }
      reporter = {
        code    = "reporter"
        timeout = 60
        memory  = 256
        env = tomap({
          TABLE       = aws_dynamodb_table.main["shop"].name
          TOPIC_ARN   = aws_sns_topic.orders.arn
          DATA_BUCKET = aws_s3_bucket.data.id
        })
        statements = jsonencode([
          { Effect = "Allow", Action = ["dynamodb:Query", "dynamodb:GetItem"], Resource = local.table_arns["shop"] },
          { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = ["${aws_s3_bucket.data.arn}/reports/*"] },
          { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
        ])
      }
      # Purge des réservations expirées et des commandes impayées (relecture de Stripe),
      # chaque minute (règle EventBridge plus bas).
      sweeper = {
        code    = "sweeper"
        timeout = 60
        memory  = 128
        env = tomap(merge(local.stripe_env, {
          TABLE     = aws_dynamodb_table.main["shop"].name
          TOPIC_ARN = aws_sns_topic.orders.arn
        }))
        statements = jsonencode(concat([
          {
            Effect   = "Allow"
            Action   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"]
            Resource = local.table_arns["shop"]
          },
          { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
        ], local.stripe_statements))
      }
    } : {},
    var.deploy_hybrid ? {
      # Lecture seule : aucune écriture possible, ni DynamoDB, ni S3, ni SNS.
      hybrid-read = {
        code    = "api"
        timeout = 15
        memory  = 256
        env = tomap(merge(local.auth_env, local.stripe_public_env, {
          TABLE         = aws_dynamodb_table.main["hybrid"].name
          ORIGIN_SECRET = random_password.origin_secret.result
          BACKEND       = "hybrid"
          READ_ONLY     = "1"
          FULFILLMENT   = "step-functions"
          IMAGES_URL    = "/images"
          REALTIME_URL  = local.realtime_url
        }))
        statements = jsonencode([
          { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query"], Resource = local.table_arns["hybrid"] },
        ])
      }
      # Rapport quotidien sur le modèle de lecture (stock, commandes et statuts à jour).
      hybrid-reporter = {
        code    = "reporter"
        timeout = 60
        memory  = 256
        env = tomap({
          TABLE       = aws_dynamodb_table.main["hybrid"].name
          SOURCE      = "hybrid"
          TOPIC_ARN   = aws_sns_topic.orders.arn
          DATA_BUCKET = aws_s3_bucket.data.id
        })
        statements = jsonencode([
          { Effect = "Allow", Action = ["dynamodb:Query", "dynamodb:GetItem"], Resource = local.table_arns["hybrid"] },
          { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = ["${aws_s3_bucket.data.arn}/reports/*"] },
          { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
        ])
      }
      hybrid-processor = {
        code    = "processor"
        timeout = 30
        memory  = 128
        env = tomap({
          TABLE             = aws_dynamodb_table.main["hybrid"].name
          SOURCE            = "hybrid"
          STATE_MACHINE_ARN = local.state_machine_arns["hybrid"]
        })
        statements = jsonencode([
          # PutItem : crée la commande depuis ORDER_PAID si la projection n'est pas encore passée.
          { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem"], Resource = local.table_arns["hybrid"] },
          { Effect = "Allow", Action = ["states:StartExecution"], Resource = [local.state_machine_arns["hybrid"]] },
          { Effect = "Allow", Action = local.sqs_read, Resource = [aws_sqs_queue.main["hybrid-processing"].arn] },
        ])
      }
      hybrid-fulfillment = {
        code    = "fulfillment"
        timeout = 30
        memory  = 128
        env = tomap({
          TABLE     = aws_dynamodb_table.main["hybrid"].name
          TOPIC_ARN = aws_sns_topic.orders.arn
          SOURCE    = "hybrid"
        })
        statements = local.fulfillment_statements["hybrid"]
      }
      hybrid-projector = {
        code    = "projector"
        timeout = 30
        memory  = 128
        env     = tomap({ TABLE = aws_dynamodb_table.main["hybrid"].name })
        statements = jsonencode([
          { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = local.table_arns["hybrid"] },
          { Effect = "Allow", Action = local.sqs_read, Resource = [aws_sqs_queue.main["hybrid-projection"].arn] },
        ])
      }
    } : {},
    # E-mails SES : seulement si une adresse est vérifiée (notification_email).
    local.mail_enabled ? {
      mailer = {
        code    = "mailer"
        timeout = 30
        memory  = 128
        env = tomap({
          MAIL_FROM     = var.notification_email
          MAIL_ADMIN    = var.notification_email
          MAIL_REDIRECT = var.notification_email # SES sandbox : seule adresse autorisée
          # Domaines réservés à la documentation (RFC 2606), utilisés par les commandes de
          # test : rien à livrer, et le bac à sable SES ne permet que 200 envois par jour.
          MAIL_SKIP_DOMAINS = "example.com,example.org,example.net"
        })
        statements = jsonencode([
          { Effect = "Allow", Action = ["ses:SendRawEmail"], Resource = [local.ses_identity_arn] },
          { Effect = "Allow", Action = local.sqs_read, Resource = [aws_sqs_queue.main["mailer"].arn] },
        ])
      }
    } : {},
    {
      audit = {
        code    = "audit"
        timeout = 30
        memory  = 128
        env     = tomap({ AUDIT_BUCKET = aws_s3_bucket.audit.id })
        statements = jsonencode([
          { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.audit.arn}/events/*"] },
          { Effect = "Allow", Action = local.sqs_read, Resource = [aws_sqs_queue.main["audit"].arn] },
        ])
      }
      # Zip à part (data.archive_file.thumbnailer, media.tf) : handler + Pillow arm64.
      thumbnailer = {
        code    = "thumbnailer"
        timeout = 30
        memory  = 512
        env     = tomap({})
        statements = jsonencode([
          { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.images.arn}/originals/*"] },
          { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.images.arn}/images/*"] },
        ])
      }
      realtime = {
        code    = "realtime"
        timeout = 10
        memory  = 128
        env     = tomap(merge(local.auth_env, { REALTIME_TABLE = aws_dynamodb_table.realtime.name }))
        statements = jsonencode([
          {
            Effect   = "Allow"
            Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
            Resource = [aws_dynamodb_table.realtime.arn]
          },
        ])
      }
      notifier = {
        code    = "notifier"
        timeout = 30
        memory  = 128
        env = tomap({
          REALTIME_TABLE  = aws_dynamodb_table.realtime.name
          CONNECTIONS_URL = local.connections_url
        })
        statements = jsonencode([
          { Effect = "Allow", Action = ["dynamodb:Query", "dynamodb:DeleteItem"], Resource = [aws_dynamodb_table.realtime.arn] },
          {
            Effect   = "Allow"
            Action   = ["execute-api:ManageConnections"]
            Resource = ["${aws_apigatewayv2_api.realtime.execution_arn}/live/POST/@connections/*"]
          },
          { Effect = "Allow", Action = local.sqs_read, Resource = [aws_sqs_queue.main["realtime"].arn] },
        ])
      }
    },
  )

  # Files SQS qui déclenchent une Lambda : fonction => file
  lambda_triggers = merge(
    var.deploy_serverless ? { processor = "processing" } : {},
    var.deploy_hybrid ? { hybrid-processor = "hybrid-processing", hybrid-projector = "hybrid-projection" } : {},
    { audit = "audit", notifier = "realtime" },
    local.mail_enabled ? { mailer = "mailer" } : {},
  )

  # Archive de chaque fonction (la thumbnailer a la sienne, avec ses dépendances).
  lambda_zips = merge(
    { for k, z in data.archive_file.lambda : k => { path = z.output_path, hash = z.output_base64sha256 } },
    { thumbnailer = { path = data.archive_file.thumbnailer.output_path, hash = data.archive_file.thumbnailer.output_base64sha256 } },
  )
}

# Zip = handler de la fonction + paquet commun shop/ (routes, règles, accès aux données).
data "archive_file" "lambda" {
  for_each    = { for k, v in local.lambdas : k => v if v.code != "thumbnailer" }
  type        = "zip"
  output_path = "${path.module}/.build/${each.key}.zip"

  source {
    content  = file("${local.src_dir}/lambda/${each.value.code}/handler.py")
    filename = "handler.py"
  }

  dynamic "source" {
    for_each = fileset("${local.src_dir}/shop", "*.py")
    content {
      content  = file("${local.src_dir}/shop/${source.value}")
      filename = "shop/${source.value}"
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each = local.lambdas
  name     = "${local.name}-lambda-${each.key}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.lambdas
  name     = "least-privilege"
  role     = aws_iam_role.lambda[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.lambda[each.key].arn}:*"]
      }],
      jsondecode(each.value.statements),
      local.kms_statements,
      local.xray_statements,
    )
  })
}

# Groupes de logs créés par Terraform => supprimés avec le reste au destroy.
resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.lambdas
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "main" {
  for_each         = local.lambdas
  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = local.lambda_zips[each.key].path
  source_code_hash = local.lambda_zips[each.key].hash
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  architectures    = ["arm64"]

  # X-Ray (lot 4) : une trace par invocation, avec les appels AWS (offre gratuite :
  # 100 000 traces enregistrées par mois).
  tracing_config {
    mode = "Active"
  }

  environment {
    variables = each.value.env
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda[each.key].name
  }

  depends_on = [aws_iam_role_policy.lambda]
}

# Fenêtre de regroupement à 0 : la Lambda est appelée dès qu'un message est reçu.
# Avec une fenêtre non nulle, le poller prolongeait son long polling (20 s) pour
# compléter le lot : mesuré à ~20 s de latence par étape quand le trafic est faible.
resource "aws_lambda_event_source_mapping" "sqs" {
  for_each                           = local.lambda_triggers
  event_source_arn                   = aws_sqs_queue.main[each.value].arn
  function_name                      = aws_lambda_function.main[each.key].arn
  batch_size                         = 10
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  # Au plus 2 exécutions simultanées par file : le compte n'en a que 10, partagées par
  # toutes les Lambdas. Sans plafond, une file chargée (le mailer freiné par SES) les
  # prenait toutes et l'API répondait 429 (session finale, annexe E du rapport).
  scaling_config {
    maximum_concurrency = 2
  }
}

# ---------------------------------------------------------------------------
# Function URL de l'API (gratuite, remplace API Gateway). Appelée uniquement
# par CloudFront, qui ajoute l'en-tête secret X-Origin-Verify.
# ---------------------------------------------------------------------------

resource "aws_lambda_function_url" "api" {
  count              = var.deploy_serverless ? 1 : 0
  function_name      = aws_lambda_function.main["api"].function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "api_url" {
  count                  = var.deploy_serverless ? 1 : 0
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.main["api"].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# Depuis fin 2025, une Function URL publique exige aussi lambda:InvokeFunction
# (limité aux appels passant par la Function URL).
resource "aws_lambda_permission" "api_invoke" {
  count                    = var.deploy_serverless ? 1 : 0
  statement_id             = "AllowInvokeViaFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.main["api"].function_name
  principal                = "*"
  invoked_via_function_url = true
}

# Même chose pour la Lambda de lecture de la solution hybride.
resource "aws_lambda_function_url" "hybrid_read" {
  count              = var.deploy_hybrid ? 1 : 0
  function_name      = aws_lambda_function.main["hybrid-read"].function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "hybrid_read_url" {
  count                  = var.deploy_hybrid ? 1 : 0
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.main["hybrid-read"].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "hybrid_read_invoke" {
  count                    = var.deploy_hybrid ? 1 : 0
  statement_id             = "AllowInvokeViaFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.main["hybrid-read"].function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------------------------------------------------------------------------
# Purge des réservations expirées (serverless) : règle EventBridge chaque minute
# (gratuite ; environ 43 000 appels Lambda par mois, dans le Free Tier). Pour la VM
# et l'hybride, la purge tourne sur les EC2, où se font les écritures de stock.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "sweeper" {
  count               = var.deploy_serverless ? 1 : 0
  name                = "${local.name}-sweeper"
  description         = "Libère les réservations de panier expirées (solution serverless)"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "sweeper" {
  count = var.deploy_serverless ? 1 : 0
  rule  = aws_cloudwatch_event_rule.sweeper[0].name
  arn   = aws_lambda_function.main["sweeper"].arn
}

resource "aws_lambda_permission" "sweeper" {
  count         = var.deploy_serverless ? 1 : 0
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.main["sweeper"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweeper[0].arn
}
