# ---------------------------------------------------------------------------
# Modèle événementiel (pub/sub) :
#
#   API (Lambda ou EC2) --publish--> SNS "order-events"
#        |-- SQS mailer         -> Lambda mailer -> SES   [confirmation, expédition, livraison...]
#        |-- SQS processing     -> Lambda processor -> Step Functions [source = serverless, ORDER_PAID, STOCK_LOW]
#        |-- SQS vm-processing  -> worker sur EC2 (chaque étape, réappro) [source = vm]
#        |-- SQS hybrid-restock -> worker sur EC2 hybride (réappro, RDS)  [source = hybrid]
#        |-- SQS hybrid-projection -> Lambda projector -> DynamoDB  [source = hybrid, écritures RDS]
#        |-- SQS hybrid-processing -> Lambda processor -> Step Functions [source = hybrid, ORDER_PAID]
#        |-- SQS audit          -> Lambda audit -> S3     [tous les événements]
#        |-- SQS realtime       -> Lambda notifier -> WebSocket [ORDER_*, BACK_IN_STOCK]
#        '-- SQS kafka-bridge   -> EC2 -> MSK (Kafka)     [si enable_msk]
#
# Chaque file a sa Dead Letter Queue (après 3 échecs) surveillée par CloudWatch.
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "orders" {
  name              = "${local.name}-order-events"
  kms_master_key_id = var.enable_kms_cmk ? aws_kms_key.main[0].arn : "alias/aws/sns"
}

locals {
  # filter = politique de filtrage SNS en JSON (null = reçoit tous les événements).
  queues = merge(
    var.deploy_serverless ? {
      # Démarre seulement les workflows (commande, réappro) : les délais sont dans les machines d'état.
      processing = {
        delay  = 0
        filter = jsonencode({ source = ["serverless"], event_type = ["ORDER_PAID", "STOCK_LOW"] })
      }
    } : {},
    var.deploy_vm ? {
      # Worker EC2 : une étape par message, le délai de la file simule le temps passé
      # (préparation, transport, délai du fournisseur).
      vm-processing = {
        delay = var.fulfillment_delay_seconds
        filter = jsonencode({
          source     = ["vm"]
          event_type = ["ORDER_PAID", "ORDER_PACKED", "ORDER_SHIPPED", "ORDER_IN_TRANSIT", "STOCK_LOW", "PURCHASE_ORDER_CREATED"]
        })
      }
    } : {},
    var.deploy_hybrid ? {
      # Sans délai : le catalogue et le suivi DynamoDB doivent rattraper RDS au plus vite.
      hybrid-projection = {
        delay = 0
        filter = jsonencode({
          source = ["hybrid"]
          event_type = ["ORDER_CREATED", "ORDER_CANCELLED", "STOCK_CHANGED", "PRODUCT_CHANGED",
            "PROMO_CHANGED", "REVIEW_ADDED", "WISHLIST_CHANGED", "BACK_IN_STOCK", "ORDER_RETURNED",
          "PURCHASE_ORDER_CREATED", "PURCHASE_ORDER_RECEIVED"]
        })
      }
      # Réappro de l'hybride : écritures de stock dans RDS, donc par l'EC2 (pas de Lambda).
      hybrid-restock = {
        delay  = var.fulfillment_delay_seconds
        filter = jsonencode({ source = ["hybrid"], event_type = ["STOCK_LOW", "PURCHASE_ORDER_CREATED"] })
      }
      hybrid-processing = {
        delay  = 0
        filter = jsonencode({ source = ["hybrid"], event_type = ["ORDER_PAID"] })
      }
    } : {},
    {
      audit = { delay = 0, filter = null }
      # Suivi en temps réel (toutes solutions) : Lambda notifier -> WebSocket.
      realtime = {
        delay = 0
        filter = jsonencode({
          event_type = ["ORDER_CREATED", "ORDER_PAID", "ORDER_PACKED", "ORDER_SHIPPED", "ORDER_IN_TRANSIT",
            "ORDER_DELIVERED", "ORDER_CANCELLED", "ORDER_REFUNDED", "ORDER_RETURN_REQUESTED", "ORDER_RETURNED",
          "BACK_IN_STOCK"]
        })
      }
    },
    local.mail_enabled ? {
      mailer = {
        delay = 0
        filter = jsonencode({
          event_type = ["ORDER_PAID", "ORDER_SHIPPED", "ORDER_DELIVERED", "ORDER_CANCELLED", "ORDER_REFUNDED",
          "ORDER_RETURN_REQUESTED", "STOCK_LOW", "PURCHASE_ORDER_CREATED", "DAILY_REPORT", "BACK_IN_STOCK"]
        })
      }
    } : {},
    var.enable_msk ? {
      kafka-bridge = { delay = 0, filter = null }
    } : {},
  )
}

resource "aws_sqs_queue" "dlq" {
  for_each                  = local.queues
  name                      = "${local.name}-${each.key}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "main" {
  for_each                   = local.queues
  name                       = "${local.name}-${each.key}"
  delay_seconds              = each.value.delay
  visibility_timeout_seconds = 60
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "main" {
  for_each  = local.queues
  queue_url = aws_sqs_queue.main[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowSNS"
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.main[each.key].arn
      Condition = {
        ArnEquals = { "aws:SourceArn" = aws_sns_topic.orders.arn }
      }
    }]
  })
}

resource "aws_sns_topic_subscription" "queues" {
  for_each             = local.queues
  topic_arn            = aws_sns_topic.orders.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.main[each.key].arn
  raw_message_delivery = true
  filter_policy        = each.value.filter

  depends_on = [aws_sqs_queue_policy.main]
}

# Topic séparé, non chiffré, pour les alarmes CloudWatch (CloudWatch ne peut pas
# publier sur un topic chiffré avec la clé gérée aws/sns).
resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.notification_email
}
