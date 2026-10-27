# ---------------------------------------------------------------------------
# Fonctions Lambda (Python 3.12, uniquement la bibliothèque standard + boto3).
#   api       : API REST de la boutique (solution serverless)
#   processor : traitement des commandes, déclenché par SQS (solution serverless)
#   audit     : archivage S3 de tous les événements (commun à toutes les solutions)
# Solution hybride (même code, autres tables, droits réduits) :
#   hybrid-read      : code "api" en lecture seule (catalogue, suivi des commandes)
#   hybrid-processor : code "processor" sur les tables hybrides
#   hybrid-projector : recopie dans DynamoDB les commandes et le stock venant de RDS
# "code" = dossier de src/lambda/ dont le handler est déployé.
# ---------------------------------------------------------------------------

locals {
  # env (map de chaînes) et statements (JSON) ont le même type pour toutes les
  # fonctions : nécessaire pour que le merge conditionnel soit bien typé.
  lambdas = merge(
    var.deploy_serverless ? {
      api = {
        code    = "api"
        timeout = 10
        memory  = 256
        env = tomap({
          PRODUCTS_TABLE = aws_dynamodb_table.products[0].name
          ORDERS_TABLE   = aws_dynamodb_table.orders[0].name
          TOPIC_ARN      = aws_sns_topic.orders.arn
          ORIGIN_SECRET  = random_password.origin_secret.result
        })
        statements = jsonencode([
          {
            Effect   = "Allow"
            Action   = ["dynamodb:GetItem", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem"]
            Resource = [aws_dynamodb_table.products[0].arn, aws_dynamodb_table.orders[0].arn]
          },
          { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
        ])
      }
      processor = {
        code    = "processor"
        timeout = 30
        memory  = 128
        env = tomap({
          ORDERS_TABLE = aws_dynamodb_table.orders[0].name
          TOPIC_ARN    = aws_sns_topic.orders.arn
        })
        statements = jsonencode([
          { Effect = "Allow", Action = ["dynamodb:UpdateItem", "dynamodb:GetItem"], Resource = [aws_dynamodb_table.orders[0].arn] },
          { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
          {
            Effect   = "Allow"
            Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
            Resource = [aws_sqs_queue.main["processing"].arn]
          },
        ])
      }
    } : {},
    var.deploy_hybrid ? {
      hybrid-read = {
        code    = "api"
        timeout = 10
        memory  = 256
        env = tomap({
          PRODUCTS_TABLE = aws_dynamodb_table.hybrid_products[0].name
          ORDERS_TABLE   = aws_dynamodb_table.hybrid_orders[0].name
          ORIGIN_SECRET  = random_password.origin_secret.result
          BACKEND        = "hybrid"
          READ_ONLY      = "1"
        })
        statements = jsonencode([
          {
            Effect   = "Allow"
            Action   = ["dynamodb:GetItem", "dynamodb:Scan"]
            Resource = [aws_dynamodb_table.hybrid_products[0].arn, aws_dynamodb_table.hybrid_orders[0].arn]
          },
        ])
      }
      hybrid-processor = {
        code    = "processor"
        timeout = 30
        memory  = 128
        env = tomap({
          ORDERS_TABLE = aws_dynamodb_table.hybrid_orders[0].name
          TOPIC_ARN    = aws_sns_topic.orders.arn
          SOURCE       = "hybrid"
        })
        statements = jsonencode([
          # PutItem : crée la commande depuis ORDER_CREATED si la projection n'est pas encore passée.
          { Effect = "Allow", Action = ["dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:PutItem"], Resource = [aws_dynamodb_table.hybrid_orders[0].arn] },
          { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
          {
            Effect   = "Allow"
            Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
            Resource = [aws_sqs_queue.main["hybrid-processing"].arn]
          },
        ])
      }
      hybrid-projector = {
        code    = "projector"
        timeout = 30
        memory  = 128
        env = tomap({
          PRODUCTS_TABLE = aws_dynamodb_table.hybrid_products[0].name
          ORDERS_TABLE   = aws_dynamodb_table.hybrid_orders[0].name
        })
        statements = jsonencode([
          { Effect = "Allow", Action = ["dynamodb:UpdateItem"], Resource = [aws_dynamodb_table.hybrid_products[0].arn] },
          { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = [aws_dynamodb_table.hybrid_orders[0].arn] },
          {
            Effect   = "Allow"
            Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
            Resource = [aws_sqs_queue.main["hybrid-projection"].arn]
          },
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
          {
            Effect   = "Allow"
            Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
            Resource = [aws_sqs_queue.main["audit"].arn]
          },
        ])
      }
    },
  )

  # Files SQS qui déclenchent une Lambda : fonction => file
  lambda_triggers = merge(
    var.deploy_serverless ? { processor = "processing" } : {},
    var.deploy_hybrid ? { hybrid-processor = "hybrid-processing", hybrid-projector = "hybrid-projection" } : {},
    { audit = "audit" },
  )
}

data "archive_file" "lambda" {
  for_each    = local.lambdas
  type        = "zip"
  source_file = "${local.src_dir}/lambda/${each.value.code}/handler.py"
  output_path = "${path.module}/.build/${each.key}.zip"
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
  filename         = data.archive_file.lambda[each.key].output_path
  source_code_hash = data.archive_file.lambda[each.key].output_base64sha256
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  architectures    = ["arm64"]

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
