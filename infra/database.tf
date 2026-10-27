# ---------------------------------------------------------------------------
# DynamoDB (solution serverless) : capacité provisionnée faible pour rester
# dans le Free Tier permanent (25 RCU / 25 WCU au total sur le compte).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "products" {
  count          = var.deploy_serverless ? 1 : 0
  name           = "${local.name}-products"
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 2
  hash_key       = "id"

  attribute {
    name = "id"
    type = "S"
  }

  server_side_encryption {
    enabled     = var.enable_kms_cmk
    kms_key_arn = local.kms_key_arn
  }
}

resource "aws_dynamodb_table" "orders" {
  count          = var.deploy_serverless ? 1 : 0
  name           = "${local.name}-orders"
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 5
  hash_key       = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  server_side_encryption {
    enabled     = var.enable_kms_cmk
    kms_key_arn = local.kms_key_arn
  }
}

# Fausse base de données : le catalogue (t-shirts, pulls, baskets) est chargé
# depuis data/products.json. Un `terraform apply` remet aussi le stock à zéro.
resource "aws_dynamodb_table_item" "products" {
  for_each   = var.deploy_serverless ? { for p in local.products : p.id => p } : {}
  table_name = aws_dynamodb_table.products[0].name
  hash_key   = aws_dynamodb_table.products[0].hash_key

  item = jsonencode({
    id          = { S = each.value.id }
    category    = { S = each.value.category }
    name        = { S = each.value.name }
    description = { S = each.value.description }
    price       = { N = tostring(each.value.price) } # forme normalisée par DynamoDB (19.9, 79) : pas de faux diff
    color       = { S = each.value.color }
    hex         = { S = each.value.hex }
    sizes       = { L = [for s in each.value.sizes : { S = s }] }
    stock       = { N = tostring(each.value.stock) }
  })
}

# ---------------------------------------------------------------------------
# RDS PostgreSQL (solution VM) : Single-AZ, db.t3.micro, 20 Go => Free Tier.
# ---------------------------------------------------------------------------

resource "random_password" "db" {
  count   = var.deploy_vm ? 1 : 0
  length  = 24
  special = false
}

# Mot de passe stocké dans SSM Parameter Store (gratuit, chiffré par aws/ssm),
# lu par les instances EC2 au démarrage : rien de sensible dans le user-data.
resource "aws_ssm_parameter" "db_password" {
  count = var.deploy_vm ? 1 : 0
  name  = "/${local.name}/db/password"
  type  = "SecureString"
  value = random_password.db[0].result
}

resource "aws_db_subnet_group" "main" {
  count      = var.deploy_vm ? 1 : 0
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "db" {
  count       = var.deploy_vm ? 1 : 0
  name        = "${local.name}-db"
  description = "PostgreSQL, accessible uniquement depuis les instances applicatives"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL depuis EC2"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app[0].id]
  }
}

resource "aws_db_instance" "main" {
  count                  = var.deploy_vm ? 1 : 0
  identifier             = "${local.name}-db"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = var.db_instance_class
  allocated_storage      = 20
  storage_type           = "gp2"
  storage_encrypted      = true
  kms_key_id             = local.kms_key_arn
  db_name                = "shop"
  username               = "shop_admin"
  password               = random_password.db[0].result
  db_subnet_group_name   = aws_db_subnet_group.main[0].name
  vpc_security_group_ids = [aws_security_group.db[0].id]
  multi_az               = var.db_multi_az
  publicly_accessible    = false

  # Suppression rapide et complète (pas de snapshot final ni de sauvegardes à payer).
  backup_retention_period  = 0
  skip_final_snapshot      = true
  delete_automated_backups = true
  deletion_protection      = false
  apply_immediately        = true

  performance_insights_enabled = false
  monitoring_interval          = 0
}
