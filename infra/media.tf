# ---------------------------------------------------------------------------
# Images produit (bucket "images", privé, commun aux trois solutions) :
#   originals/<produit>/<id>.<ext>   déposé par le back-office (formulaire POST présigné,
#                                     5 Mo au plus, type imposé)
#   images/<produit>/<id>-<taille>.webp  miniatures écrites par la Lambda thumbnailer
# Les miniatures sont servies par CloudFront sous /images/* (OAC, cache long : le nom
# change à chaque nouvelle image). Les originaux ne sont jamais publics.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "images" {
  bucket        = "${local.name}-images-${local.suffix}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "images" {
  bucket                  = aws_s3_bucket.images.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "images" {
  bucket = aws_s3_bucket.images.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Le navigateur envoie l'original directement à S3 (formulaire présigné).
resource "aws_s3_bucket_cors_configuration" "images" {
  bucket = aws_s3_bucket.images.id

  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    max_age_seconds = 300
  }
}

resource "aws_cloudfront_origin_access_control" "images" {
  name                              = "${local.name}-images-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

locals {
  distribution_arns = concat(aws_cloudfront_distribution.serverless[*].arn, aws_cloudfront_distribution.vm[*].arn,
  aws_cloudfront_distribution.hybrid[*].arn)
  # Droits de l'API (Lambda api, EC2) : signer le formulaire de dépôt et vérifier l'original.
  images_statements = [
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.images.arn}/originals/*"] },
    {
      Effect    = "Allow"
      Action    = ["s3:ListBucket"]
      Resource  = [aws_s3_bucket.images.arn]
      Condition = { StringLike = { "s3:prefix" = ["originals/*"] } }
    },
  ]
}

resource "aws_s3_bucket_policy" "images" {
  count  = length(local.distribution_arns) > 0 ? 1 : 0
  bucket = aws_s3_bucket.images.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.images.arn}/images/*"
      Condition = { StringEquals = { "AWS:SourceArn" = local.distribution_arns } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.images]
}

# Dépôt d'un original -> Lambda thumbnailer (appel asynchrone, rejoué deux fois par Lambda).
resource "aws_lambda_permission" "thumbnailer" {
  statement_id   = "AllowS3"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.main["thumbnailer"].function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.images.arn
  source_account = local.account_id
}

resource "aws_s3_bucket_notification" "images" {
  bucket = aws_s3_bucket.images.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.main["thumbnailer"].arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "originals/"
  }

  depends_on = [aws_lambda_permission.thumbnailer]
}

# Lambda thumbnailer : zip préparé par scripts/build_lambda_deps.sh (handler + Pillow
# Linux arm64), appelé par up.sh et down.sh avant Terraform.
data "archive_file" "thumbnailer" {
  type        = "zip"
  source_dir  = "${path.module}/.build/thumbnailer"
  output_path = "${path.module}/.build/thumbnailer.zip"
}
