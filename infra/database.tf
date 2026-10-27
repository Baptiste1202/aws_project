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
# DynamoDB (solution hybride) : modèle de lecture (CQRS). RDS fait foi pour le
# stock et la création des commandes ; ces tables sont mises à jour par les
# événements (Lambda projector) et lues par la Lambda hybrid-read. Le statut
# des commandes, lui, est géré ici par la Lambda hybrid-processor.
# Avec les 3 solutions : 20 RCU / 14 WCU sur les 25 gratuites.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "hybrid_products" {
  count          = var.deploy_hybrid ? 1 : 0
  name           = "${local.name}-hybrid-products"
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

resource "aws_dynamodb_table" "hybrid_orders" {
  count          = var.deploy_hybrid ? 1 : 0
  name           = "${local.name}-hybrid-orders"
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

# Même catalogue initial que la base shop_hybrid (chargée par l'EC2 au démarrage).
# ignore_changes : le stock est ensuite piloté par la projection, un nouvel apply
# ne doit pas le remettre à zéro sans RDS (les deux divergeraient).
resource "aws_dynamodb_table_item" "hybrid_products" {
  for_each   = var.deploy_hybrid ? { for p in local.products : p.id => p } : {}
  table_name = aws_dynamodb_table.hybrid_products[0].name
  hash_key   = aws_dynamodb_table.hybrid_products[0].hash_key

  item = jsonencode({
    id          = { S = each.value.id }
    category    = { S = each.value.category }
    name        = { S = each.value.name }
    description = { S = each.value.description }
    price       = { N = tostring(each.value.price) }
    color       = { S = each.value.color }
    hex         = { S = each.value.hex }
    sizes       = { L = [for s in each.value.sizes : { S = s }] }
    stock       = { N = tostring(each.value.stock) }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

# ---------------------------------------------------------------------------
# RDS PostgreSQL (solutions VM et hybride, une base chacune) : Single-AZ, db.t3.micro, 20 Go.
# ---------------------------------------------------------------------------

resource "random_password" "db" {
  count   = local.ec2_enabled ? 1 : 0
  length  = 24
  special = false
}

# Mot de passe stocké dans SSM Parameter Store (gratuit, chiffré par aws/ssm),
# lu par les instances EC2 au démarrage : rien de sensible dans le user-data.
resource "aws_ssm_parameter" "db_password" {
  count = local.ec2_enabled ? 1 : 0
  name  = "/${local.name}/db/password"
  type  = "SecureString"
  value = random_password.db[0].result
}

resource "aws_db_subnet_group" "main" {
  count      = local.ec2_enabled ? 1 : 0
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "db" {
  count       = local.ec2_enabled ? 1 : 0
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
  count                  = local.ec2_enabled ? 1 : 0
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
