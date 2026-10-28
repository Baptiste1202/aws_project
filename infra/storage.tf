# ---------------------------------------------------------------------------
# Bucket "web" (privé) :
#   web/     -> site statique servi par CloudFront (solutions serverless et hybride)
#   vm-app/  -> code de l'application + front, téléchargés par les EC2 au boot
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "web" {
  bucket        = "${local.name}-web-${local.suffix}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "web" {
  count  = local.s3_front_enabled ? 1 : 0
  bucket = aws_s3_bucket.web.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.web.arn}/web/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = concat(aws_cloudfront_distribution.serverless[*].arn, aws_cloudfront_distribution.hybrid[*].arn)
        }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.web]
}

resource "aws_s3_object" "frontend" {
  for_each     = fileset(local.frontend_dir, "**")
  bucket       = aws_s3_bucket.web.id
  key          = "web/${each.value}"
  source       = "${local.frontend_dir}/${each.value}"
  etag         = filemd5("${local.frontend_dir}/${each.value}")
  content_type = lookup(local.content_types, reverse(split(".", each.value))[0], "application/octet-stream")
}

# Application EC2 : app.py et le paquet commun shop/ (copiés dans /opt/shop au démarrage).
resource "aws_s3_object" "vm_app" {
  for_each = local.ec2_enabled ? merge(
    { "app.py" = "${local.src_dir}/vm/app.py" },
    { for f in fileset("${local.src_dir}/shop", "*.py") : "shop/${f}" => "${local.src_dir}/shop/${f}" },
  ) : {}
  bucket = aws_s3_bucket.web.id
  key    = "vm-app/${each.key}"
  source = each.value
  etag   = filemd5(each.value)
}

resource "aws_s3_object" "vm_products" {
  count  = local.ec2_enabled ? 1 : 0
  bucket = aws_s3_bucket.web.id
  key    = "vm-app/products.json"
  source = "${local.root_dir}/data/products.json"
  etag   = filemd5("${local.root_dir}/data/products.json")
}

# ---------------------------------------------------------------------------
# Bucket "audit" : chaque événement de commande y est archivé en JSON,
# partitionné par date (requêtable avec Athena pour le reporting).
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "audit" {
  bucket        = "${local.name}-audit-${local.suffix}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.enable_kms_cmk ? "aws:kms" : "AES256"
      kms_master_key_id = local.kms_key_arn
    }
    bucket_key_enabled = var.enable_kms_cmk
  }
}

# ---------------------------------------------------------------------------
# Bucket "data" (privé) : exports et imports CSV du catalogue et factures, échangés
# par liens présignés de 5 min, supprimés au bout d'un jour ; rapports quotidiens
# (lot 4, reports/) gardés 30 jours.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "data" {
  bucket        = "${local.name}-data-${local.suffix}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  dynamic "rule" {
    for_each = { exports = 1, imports = 1, invoices = 1, reports = 30 }
    content {
      id     = "${rule.key}-${rule.value}j"
      status = "Enabled"
      filter {
        prefix = "${rule.key}/"
      }
      expiration {
        days = rule.value
      }
    }
  }
}

# Le back-office (navigateur) dépose le CSV directement dans S3 par lien présigné :
# CORS limité à PUT et GET (le lien signé reste la protection).
resource "aws_s3_bucket_cors_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  cors_rule {
    allowed_methods = ["GET", "PUT"]
    allowed_origins = ["*"]
    allowed_headers = ["Content-Type"]
    max_age_seconds = 300
  }
}
