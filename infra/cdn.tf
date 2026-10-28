# ---------------------------------------------------------------------------
# CloudFront : point d'entrée HTTPS unique de chaque solution.
#   Serverless : /*  -> S3 (site statique, OAC)   |  /api/* -> Lambda Function URL
#   VM         : /*  -> ALB -> EC2 (sert le site et l'API)
#   Hybride    : /*  -> S3  |  /api/orders (POST) -> ALB -> EC2 + RDS
#                /api/* (catalogue, suivi) -> Lambda hybrid-read + DynamoDB
#   Toutes      : /images/* -> S3 (bucket images, miniatures des produits, OAC)
# ---------------------------------------------------------------------------

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}

# Cache court pour les fichiers statiques : une modification du front est visible
# en moins d'une minute, sans invalidation.
resource "aws_cloudfront_cache_policy" "static" {
  name        = "${local.name}-static"
  min_ttl     = 0
  default_ttl = 60
  max_ttl     = 300

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "web" {
  count                             = local.s3_front_enabled ? 1 : 0
  name                              = "${local.name}-web-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "serverless" {
  count               = var.deploy_serverless ? 1 : 0
  enabled             = true
  comment             = "${local.name} - solution serverless"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  aliases             = local.use_custom_domain ? [local.serverless_domain] : []

  origin {
    origin_id                = "s3-web"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_path              = "/web"
    origin_access_control_id = aws_cloudfront_origin_access_control.web[0].id
  }

  origin {
    origin_id   = "lambda-api"
    domain_name = trimsuffix(trimprefix(aws_lambda_function_url.api[0].function_url, "https://"), "/")

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
    origin_id                = "s3-images"
    domain_name              = aws_s3_bucket.images.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.images.id
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

  ordered_cache_behavior {
    path_pattern               = "/api/*"
    target_origin_id           = "lambda-api"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_except_host.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  # Miniatures des images produit (bucket images, media.tf).
  ordered_cache_behavior {
    path_pattern               = "/images/*"
    target_origin_id           = "s3-images"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = false
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = !local.use_custom_domain
    acm_certificate_arn            = local.use_custom_domain ? aws_acm_certificate_validation.main[0].certificate_arn : null
    ssl_support_method             = local.use_custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.use_custom_domain ? "TLSv1.2_2021" : null
  }
}

resource "aws_cloudfront_distribution" "vm" {
  count       = var.deploy_vm ? 1 : 0
  enabled     = true
  comment     = "${local.name} - solution VM (EC2)"
  price_class = "PriceClass_100"
  aliases     = local.use_custom_domain ? [local.vm_domain] : []

  origin {
    origin_id   = "alb"
    domain_name = aws_lb.app[0].dns_name

    # Pas de certificat sur l'ALB (il faudrait un domaine) : CloudFront -> ALB en HTTP,
    # l'ALB n'accepte que les IP CloudFront et l'en-tête secret.
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_secret.result
    }
  }

  origin {
    origin_id                = "s3-images"
    domain_name              = aws_s3_bucket.images.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.images.id
  }

  default_cache_behavior {
    target_origin_id           = "alb"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  # Miniatures des images produit (bucket images, media.tf).
  ordered_cache_behavior {
    path_pattern               = "/images/*"
    target_origin_id           = "s3-images"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = false
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = !local.use_custom_domain
    acm_certificate_arn            = local.use_custom_domain ? aws_acm_certificate_validation.main[0].certificate_arn : null
    ssl_support_method             = local.use_custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.use_custom_domain ? "TLSv1.2_2021" : null
  }
}
