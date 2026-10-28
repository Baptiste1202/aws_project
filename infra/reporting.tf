# ---------------------------------------------------------------------------
# Lot 4 : pilotage.
#
#   Rapport quotidien : EventBridge Scheduler (14 millions d'appels gratuits par mois),
#     chaque matin à 6 h (Paris), une planification par solution :
#       serverless, hybride -> Lambda reporter (DynamoDB) ;
#       VM                  -> file SQS vm-processing, lue par le worker EC2 (RDS).
#     CSV dans s3://data/reports/<solution>/, e-mail SES par la Lambda mailer. À la
#     demande : bouton du back-office (POST /api/admin/reports/daily) ou
#     scripts/daily_report.sh, qui invoque la cible de la planification.
#   Athena + Glue sur le bucket d'audit : table partitionnée (event_type, date) par
#     projection de partitions (aucun crawler, aucun MSCK REPAIR : rien à exécuter ni
#     à payer quand un jour ou un type d'événement apparaît) ; requêtes nommées ;
#     résultats dans un bucket qui les supprime au bout d'un jour. Athena est facturé
#     5 $ par To lu (10 Mo au minimum par requête) : quelques millièmes de centime ici.
# ---------------------------------------------------------------------------

locals {
  report_schedules = merge(
    var.deploy_serverless ? { serverless = { arn = aws_lambda_function.main["reporter"].arn, input = "{}" } } : {},
    var.deploy_hybrid ? { hybrid = { arn = aws_lambda_function.main["hybrid-reporter"].arn, input = "{}" } } : {},
    var.deploy_vm ? {
      vm = { arn = aws_sqs_queue.main["vm-processing"].arn, input = jsonencode({ event_type = "DAILY_REPORT_REQUESTED" }) }
    } : {},
  )
  # Types d'événements archivés (projection de partitions : valeurs connues d'avance).
  audit_event_types = [
    "ORDER_CREATED", "ORDER_PAID", "ORDER_PACKED", "ORDER_SHIPPED", "ORDER_IN_TRANSIT", "ORDER_DELIVERED",
    "ORDER_CANCELLED", "ORDER_REFUNDED", "ORDER_RETURN_REQUESTED", "ORDER_RETURNED", "STOCK_CHANGED", "STOCK_LOW",
    "BACK_IN_STOCK", "PRODUCT_CHANGED", "PROMO_CHANGED", "REVIEW_ADDED", "WISHLIST_CHANGED",
    "PURCHASE_ORDER_CREATED", "PURCHASE_ORDER_RECEIVED", "DAILY_REPORT",
  ]
}

# --- Rapport quotidien ---------------------------------------------------------

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "report-targets"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [for k, s in local.report_schedules : {
        Effect   = "Allow"
        Action   = [k == "vm" ? "sqs:SendMessage" : "lambda:InvokeFunction"]
        Resource = [s.arn]
      }],
      local.kms_statements,
    )
  })
}

resource "aws_scheduler_schedule" "daily_report" {
  for_each                     = local.report_schedules
  name                         = "${local.name}-daily-report-${each.key}"
  description                  = "Rapport quotidien (ventes, stock, valorisation) de la solution ${each.key}"
  schedule_expression          = "cron(0 6 * * ? *)"
  schedule_expression_timezone = "Europe/Paris"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = each.value.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = each.value.input
    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

# --- Athena + Glue -------------------------------------------------------------

resource "aws_s3_bucket" "athena" {
  bucket        = "${local.name}-athena-${local.suffix}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "athena" {
  bucket                  = aws_s3_bucket.athena.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "athena" {
  bucket = aws_s3_bucket.athena.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "athena" {
  bucket = aws_s3_bucket.athena.id
  rule {
    id     = "resultats-1j"
    status = "Enabled"
    filter {}
    expiration {
      days = 1
    }
  }
}

resource "aws_glue_catalog_database" "audit" {
  name        = replace("${local.name}_audit", "-", "_")
  description = "Événements de la boutique archivés dans le bucket d'audit"
}

resource "aws_glue_catalog_table" "events" {
  name          = "events"
  database_name = aws_glue_catalog_database.audit.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    classification                  = "json"
    "projection.enabled"            = "true"
    "projection.event_type.type"    = "enum"
    "projection.event_type.values"  = join(",", local.audit_event_types)
    "projection.date.type"          = "date"
    "projection.date.format"        = "yyyy-MM-dd"
    "projection.date.range"         = "2026-01-01,NOW"
    "projection.date.interval"      = "1"
    "projection.date.interval.unit" = "DAYS"
    "storage.location.template"     = "s3://${aws_s3_bucket.audit.id}/events/event_type=$${event_type}/date=$${date}/"
  }

  partition_keys {
    name = "event_type"
    type = "string"
  }
  partition_keys {
    name = "date"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.audit.id}/events/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters            = { "ignore.malformed.json" = "true" }
    }

    columns {
      name = "event_id"
      type = "string"
    }
    columns {
      name = "occurred_at"
      type = "string"
    }
    columns {
      name = "source"
      type = "string"
    }
    columns {
      name = "order"
      type = "struct<order_id:string,status:string,total:double,shipping_fee:double,created_at:string>"
    }
    columns {
      name = "alert"
      type = "struct<product_id:string,available:int,threshold:int>"
    }
  }
}

resource "aws_athena_workgroup" "main" {
  name          = local.name
  force_destroy = true

  configuration {
    enforce_workgroup_configuration = true
    # Garde-fou de coût : une requête qui lirait plus de 100 Mo est annulée.
    bytes_scanned_cutoff_per_query = 100 * 1024 * 1024
    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena.id}/results/"
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

locals {
  athena_table = "${aws_glue_catalog_database.audit.name}.events"
  named_queries = {
    "chiffre-affaires-par-jour" = {
      description = "Chiffre d'affaires par jour et par solution (paiements moins remboursements)"
      query       = <<-SQL
        SELECT date, source,
               sum(CASE WHEN event_type = 'ORDER_PAID' THEN "order".total ELSE 0 END) AS encaisse,
               sum(CASE WHEN event_type = 'ORDER_REFUNDED' THEN "order".total ELSE 0 END) AS rembourse,
               sum(CASE WHEN event_type = 'ORDER_PAID' THEN "order".total ELSE -"order".total END) AS chiffre_affaires
        FROM ${local.athena_table}
        WHERE event_type IN ('ORDER_PAID', 'ORDER_REFUNDED')
        GROUP BY date, source
        ORDER BY date DESC, source
      SQL
    }
    "delai-commande-livraison" = {
      description = "Délai moyen entre la création d'une commande et sa livraison, par solution"
      query       = <<-SQL
        SELECT c.source, count(*) AS commandes_livrees,
               round(avg(date_diff('second', from_iso8601_timestamp(c.occurred_at),
                                   from_iso8601_timestamp(d.occurred_at))), 1) AS delai_moyen_s
        FROM ${local.athena_table} c
        JOIN ${local.athena_table} d ON c."order".order_id = d."order".order_id
        WHERE c.event_type = 'ORDER_CREATED' AND d.event_type = 'ORDER_DELIVERED'
        GROUP BY c.source
        ORDER BY c.source
      SQL
    }
    "annulations" = {
      description = "Annulations par jour et par solution, et part des commandes créées"
      query       = <<-SQL
        SELECT date, source,
               count_if(event_type = 'ORDER_CANCELLED') AS annulations,
               count_if(event_type = 'ORDER_CREATED') AS commandes,
               round(100.0 * count_if(event_type = 'ORDER_CANCELLED')
                     / nullif(count_if(event_type = 'ORDER_CREATED'), 0), 1) AS taux_pct
        FROM ${local.athena_table}
        WHERE event_type IN ('ORDER_CREATED', 'ORDER_CANCELLED')
        GROUP BY date, source
        ORDER BY date DESC, source
      SQL
    }
  }
}

resource "aws_athena_named_query" "main" {
  for_each    = local.named_queries
  name        = each.key
  description = each.value.description
  workgroup   = aws_athena_workgroup.main.id
  database    = aws_glue_catalog_database.audit.name
  query       = each.value.query
}
