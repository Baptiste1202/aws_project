output "serverless_url" {
  description = "Boutique - solution serverless (CloudFront + S3 + Lambda + DynamoDB)."
  value = var.deploy_serverless ? (
    local.use_custom_domain ? "https://${local.serverless_domain}" : "https://${aws_cloudfront_distribution.serverless[0].domain_name}"
  ) : null
}

output "vm_url" {
  description = "Boutique - solution VM (CloudFront + ALB + EC2 + RDS). Compter ~3 min après l'apply pour le démarrage des instances."
  value = var.deploy_vm ? (
    local.use_custom_domain ? "https://${local.vm_domain}" : "https://${aws_cloudfront_distribution.vm[0].domain_name}"
  ) : null
}

output "dashboard_url" {
  description = "Tableau de bord CloudWatch."
  value       = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
}

output "audit_bucket" {
  description = "Bucket S3 contenant l'historique de tous les événements de commande."
  value       = aws_s3_bucket.audit.id
}

output "sns_topic_arn" {
  description = "Topic SNS des événements de commande."
  value       = aws_sns_topic.orders.arn
}

output "msk_bootstrap_brokers" {
  description = "Brokers Kafka (si enable_msk)."
  value       = var.enable_msk ? aws_msk_cluster.main[0].bootstrap_brokers : null
}
