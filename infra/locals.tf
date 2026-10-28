data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "random_id" "suffix" {
  byte_length = 3
}

# Secret partagé entre CloudFront et les origines (Lambda URL / ALB) : empêche
# de contourner CloudFront en appelant directement l'origine.
resource "random_password" "origin_secret" {
  length  = 32
  special = false
}

locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  suffix     = random_id.suffix.hex
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)

  root_dir     = "${path.module}/.."
  src_dir      = "${local.root_dir}/src"
  frontend_dir = "${local.src_dir}/frontend"
  products     = jsondecode(file("${local.root_dir}/data/products.json"))

  use_custom_domain = var.route53_zone_name != ""
  serverless_domain = "shop.${var.route53_zone_name}"
  vm_domain         = "vm.${var.route53_zone_name}"

  kms_key_arn = var.enable_kms_cmk ? aws_kms_key.main[0].arn : null

  # Réseau, ALB, EC2 et RDS servent à la solution VM et à la solution hybride.
  ec2_enabled = var.deploy_vm || var.deploy_hybrid
  # Le bucket web (OAC) sert les solutions serverless et hybride.
  s3_front_enabled = var.deploy_serverless || var.deploy_hybrid

  # Services lancés sur chaque EC2 : un par solution (même app.py, autre base et port).
  ec2_services = concat(
    var.deploy_vm ? [{
      unit         = "shop"
      source       = "vm"
      port         = 8080
      db_name      = "shop"
      queue_url    = aws_sqs_queue.main["vm-processing"].url
      status_table = ""
    }] : [],
    var.deploy_hybrid ? [{
      unit    = "shop-hybrid"
      source  = "hybrid"
      port    = 8081
      db_name = "shop_hybrid"
      # Worker de réappro (écritures de stock dans RDS).
      queue_url = aws_sqs_queue.main["hybrid-restock"].url
      # Le statut des commandes fait foi dans le modèle de lecture DynamoDB (annulation).
      status_table = "${local.name}-hybrid"
    }] : [],
  )

  # Politique IAM réutilisable pour utiliser la clé KMS du projet (si activée).
  kms_statements = var.enable_kms_cmk ? [{
    Effect   = "Allow"
    Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
    Resource = [aws_kms_key.main[0].arn]
  }] : []

  content_types = {
    html = "text/html; charset=utf-8"
    css  = "text/css; charset=utf-8"
    js   = "application/javascript; charset=utf-8"
    json = "application/json"
    py   = "text/x-python"
    svg  = "image/svg+xml"
    ico  = "image/x-icon"
  }
}
