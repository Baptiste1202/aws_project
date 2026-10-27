# ---------------------------------------------------------------------------
# Bucket "web" (privé) :
#   web/     -> site statique servi par CloudFront (solution serverless)
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
  count  = var.deploy_serverless ? 1 : 0
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
        StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.serverless[0].arn }
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

resource "aws_s3_object" "vm_app" {
  for_each = var.deploy_vm ? toset(["app.py"]) : toset([])
  bucket   = aws_s3_bucket.web.id
  key      = "vm-app/${each.value}"
  source   = "${local.src_dir}/vm/${each.value}"
  etag     = filemd5("${local.src_dir}/vm/${each.value}")
}

resource "aws_s3_object" "vm_products" {
  count  = var.deploy_vm ? 1 : 0
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
