# ---------------------------------------------------------------------------
# CloudWatch : alarmes (10 gratuites) + tableau de bord (3 gratuits).
# Les alarmes notifient le topic SNS "alerts" (e-mail si notification_email).
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "dlq" {
  for_each            = local.queues
  alarm_name          = "${local.name}-${each.key}-dlq-not-empty"
  alarm_description   = "Des messages de la file ${each.key} ont échoué 3 fois et sont en DLQ."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq[each.key].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = local.lambdas
  alarm_name          = "${local.name}-lambda-${each.key}-errors"
  alarm_description   = "Erreurs de la Lambda ${each.key}."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.main[each.key].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "alb_unhealthy" {
  count               = var.deploy_vm ? 1 : 0
  alarm_name          = "${local.name}-alb-no-healthy-host"
  alarm_description   = "Aucune instance EC2 saine derrière l'ALB : le site VM est indisponible."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.app[0].arn_suffix, TargetGroup = aws_lb_target_group.app[0].arn_suffix }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name

  dashboard_body = jsonencode({
    widgets = concat(
      [
        {
          type = "metric", x = 0, y = 0, width = 12, height = 6
          properties = {
            title   = "Commandes publiées (SNS)"
            region  = var.region
            stat    = "Sum"
            period  = 60
            metrics = [["AWS/SNS", "NumberOfMessagesPublished", "TopicName", aws_sns_topic.orders.name]]
          }
        },
        {
          type = "metric", x = 12, y = 0, width = 12, height = 6
          properties = {
            title   = "Messages en attente (SQS)"
            region  = var.region
            stat    = "Maximum"
            period  = 60
            metrics = [for k, q in aws_sqs_queue.main : ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", q.name]]
          }
        },
        {
          type = "metric", x = 0, y = 6, width = 12, height = 6
          properties = {
            title  = "Lambda : invocations / erreurs"
            region = var.region
            stat   = "Sum"
            period = 60
            metrics = concat(
              [for f in aws_lambda_function.main : ["AWS/Lambda", "Invocations", "FunctionName", f.function_name]],
              [for f in aws_lambda_function.main : ["AWS/Lambda", "Errors", "FunctionName", f.function_name]],
            )
          }
        },
      ],
      var.deploy_vm ? [
        {
          type = "metric", x = 12, y = 6, width = 12, height = 6
          properties = {
            title  = "Solution VM : requêtes ALB / CPU EC2 / CPU RDS"
            region = var.region
            period = 60
            metrics = [
              ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.app[0].arn_suffix, { stat = "Sum" }],
              ["AWS/EC2", "CPUUtilization", "AutoScalingGroupName", aws_autoscaling_group.app[0].name, { stat = "Average", yAxis = "right" }],
              ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.main[0].identifier, { stat = "Average", yAxis = "right" }],
            ]
          }
        },
      ] : [],
    )
  })
}

# ---------------------------------------------------------------------------
# Coûts : budget mensuel avec alerte e-mail (2 budgets gratuits par compte).
# Le détail des coûts se consulte dans Cost Explorer, filtré par le tag
# Project = <project> (appliqué à toutes les ressources via default_tags).
# ---------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  count        = var.notification_email != "" ? 1 : 0
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.notification_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.notification_email]
  }
}
