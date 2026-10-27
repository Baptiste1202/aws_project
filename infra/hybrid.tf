# ---------------------------------------------------------------------------
# Solution 3 : hybride. Chaque partie sur le service qui lui convient le mieux,
# avec les ressources partagées avec les solutions 1 et 2 :
#   /*            -> S3 (même bucket et même OAC que la solution serverless)
#   /api/orders   -> ALB -> EC2 (service shop-hybrid, port 8081) -> RDS (base shop_hybrid)
#                    écriture : transaction SQL, sans limite de concurrence Lambda
#   /api/*        -> Lambda hybrid-read -> DynamoDB (catalogue, suivi des commandes)
# RDS fait foi pour le stock et la création des commandes ; DynamoDB pour le statut.
# Les événements SNS (source = hybrid) relient les deux (CQRS, cohérence à terme) :
# voir messaging.tf (files hybrid-*) et lambda.tf (hybrid-processor, hybrid-projector).
# ---------------------------------------------------------------------------

resource "aws_cloudfront_distribution" "hybrid" {
  count               = var.deploy_hybrid ? 1 : 0
  enabled             = true
  comment             = "${local.name} - solution hybride"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"

  origin {
    origin_id                = "s3-web"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_path              = "/web"
    origin_access_control_id = aws_cloudfront_origin_access_control.web[0].id
  }

  origin {
    origin_id   = "lambda-read"
    domain_name = trimsuffix(trimprefix(aws_lambda_function_url.hybrid_read[0].function_url, "https://"), "/")

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_secret.result
    }
  }

  origin {
    origin_id   = "alb"
    domain_name = aws_lb.app[0].dns_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    # La règle ALB "hybrid" exige les deux en-têtes.
    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_secret.result
    }

    custom_header {
      name  = "X-Shop"
      value = "hybrid"
    }
  }

  default_cache_behavior {
    target_origin_id           = "s3-web"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.static.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  # Chemin exact (sans joker) : seule la création de commande va sur EC2 + RDS,
  # le suivi /api/orders/{id} est servi par la Lambda (règle suivante).
  ordered_cache_behavior {
    path_pattern               = "/api/orders"
    target_origin_id           = "alb"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  ordered_cache_behavior {
    path_pattern               = "/api/*"
    target_origin_id           = "lambda-read"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_except_host.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
