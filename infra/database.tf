# ---------------------------------------------------------------------------
# DynamoDB : une table par solution (« single-table design », voir
# src/shop/dynamo_store.py) : produits, stock par entrepôt, commandes, mouvements,
# réservations. Clés pk / sk, index gsi1 (gsi1pk = type, gsi1sk = tri).
#   shop   : solution serverless (fait foi)
#   hybrid : modèle de lecture de la solution hybride (CQRS) : RDS fait foi pour le
#            stock et les produits, la table est mise à jour par la Lambda projector ;
#            le statut des commandes, lui, fait foi ici.
# Facturation à la demande : avec l'index, la capacité provisionnée dépasserait les
# 25 RCU/WCU gratuites et serait facturée à l'heure ; à la demande, on paie à la
# requête (fractions de centime aux volumes du projet), jamais à l'heure.
# ---------------------------------------------------------------------------

locals {
  dynamo_tables = merge(
    var.deploy_serverless ? { shop = "${local.name}-shop" } : {},
    var.deploy_hybrid ? { hybrid = "${local.name}-hybrid" } : {},
  )

  # Catalogue initial (même règle que shop.domain : 2/3 Paris, 1/3 Lyon).
  seed_products = { for p in local.products : p.id => p }
  seed_stock = merge([for p in local.products : {
    "${p.id}#PAR" = { product_id = p.id, warehouse = "PAR", on_hand = p.stock - floor(p.stock / 3) }
    "${p.id}#LYS" = { product_id = p.id, warehouse = "LYS", on_hand = floor(p.stock / 3) }
  }]...)
}

resource "aws_dynamodb_table" "main" {
  for_each     = local.dynamo_tables
  name         = each.value
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  dynamic "attribute" {
    for_each = ["pk", "sk", "gsi1pk", "gsi1sk"]
    content {
      name = attribute.value
      type = "S"
    }
  }

  global_secondary_index {
    name            = "gsi1"
    projection_type = "ALL"

    key_schema {
      attribute_name = "gsi1pk"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "gsi1sk"
      key_type       = "RANGE"
    }
  }

  # Les réservations expirées sont libérées par la purge ; le TTL supprime ensuite
  # les éléments restants (le TTL seul ne rendrait pas le stock réservé).
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = var.enable_kms_cmk
    kms_key_arn = local.kms_key_arn
  }
}

# Catalogue initial, écrit à la création de la table seulement (ignore_changes) :
# ensuite, le stock évolue avec les ventes, les mouvements et les réservations, et le
# remettre à zéro à chaque apply le rendrait incohérent avec le journal (et, en
# hybride, avec RDS).
resource "aws_dynamodb_table_item" "product" {
  for_each   = { for pair in setproduct(keys(local.dynamo_tables), keys(local.seed_products)) : "${pair[0]}/${pair[1]}" => { table = pair[0], p = local.seed_products[pair[1]] } }
  table_name = aws_dynamodb_table.main[each.value.table].name
  hash_key   = "pk"
  range_key  = "sk"

  item = jsonencode({
    pk                  = { S = "PRODUCT#${each.value.p.id}" }
    sk                  = { S = "META" }
    gsi1pk              = { S = "PRODUCT" }
    gsi1sk              = { S = each.value.p.id }
    entity              = { S = "PRODUCT" }
    id                  = { S = each.value.p.id }
    category            = { S = each.value.p.category }
    name                = { S = each.value.p.name }
    description         = { S = each.value.p.description }
    price               = { N = tostring(each.value.p.price) }
    cost_price          = { N = tostring(each.value.p.cost_price) }
    low_stock_threshold = { N = tostring(each.value.p.low_stock_threshold) }
    archived            = { BOOL = false }
    color               = { S = each.value.p.color }
    hex                 = { S = each.value.p.hex }
    sizes               = { L = [for s in each.value.p.sizes : { S = s }] }
    version             = { N = "0" }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

resource "aws_dynamodb_table_item" "stock" {
  for_each   = { for pair in setproduct(keys(local.dynamo_tables), keys(local.seed_stock)) : "${pair[0]}/${pair[1]}" => { table = pair[0], s = local.seed_stock[pair[1]] } }
  table_name = aws_dynamodb_table.main[each.value.table].name
  hash_key   = "pk"
  range_key  = "sk"

  item = jsonencode({
    pk         = { S = "PRODUCT#${each.value.s.product_id}" }
    sk         = { S = "STOCK#${each.value.s.warehouse}" }
    gsi1pk     = { S = "STOCK" }
    gsi1sk     = { S = "${each.value.s.product_id}#${each.value.s.warehouse}" }
    entity     = { S = "STOCK" }
    product_id = { S = each.value.s.product_id }
    warehouse  = { S = each.value.s.warehouse }
    on_hand    = { N = tostring(each.value.s.on_hand) }
    reserved   = { N = "0" }
    available  = { N = tostring(each.value.s.on_hand) }
    version    = { N = "0" }
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
