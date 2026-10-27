# ---------------------------------------------------------------------------
# Route 53 (optionnel) : activé seulement si var.route53_zone_name est renseigné.
# La zone hébergée doit déjà exister (0,50 $/mois) : elle n'est pas recréée
# chaque jour, sinon il faudrait changer les serveurs DNS chez le registrar.
#   shop.<domaine> -> CloudFront serverless
#   vm.<domaine>   -> CloudFront VM
# ---------------------------------------------------------------------------

data "aws_route53_zone" "main" {
  count = local.use_custom_domain ? 1 : 0
  name  = var.route53_zone_name
}

locals {
  custom_domains = local.use_custom_domain ? concat(
    var.deploy_serverless ? [local.serverless_domain] : [],
    var.deploy_vm ? [local.vm_domain] : [],
  ) : []
}

resource "aws_acm_certificate" "main" {
  count                     = local.use_custom_domain ? 1 : 0
  provider                  = aws.us_east_1
  domain_name               = local.custom_domains[0]
  subject_alternative_names = slice(local.custom_domains, 1, length(local.custom_domains))
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = local.use_custom_domain ? {
    for o in aws_acm_certificate.main[0].domain_validation_options : o.domain_name => o
  } : {}

  zone_id         = data.aws_route53_zone.main[0].zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "main" {
  count                   = local.use_custom_domain ? 1 : 0
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.main[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_route53_record" "serverless" {
  count   = local.use_custom_domain && var.deploy_serverless ? 1 : 0
  zone_id = data.aws_route53_zone.main[0].zone_id
  name    = local.serverless_domain
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.serverless[0].domain_name
    zone_id                = aws_cloudfront_distribution.serverless[0].hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "vm" {
  count   = local.use_custom_domain && var.deploy_vm ? 1 : 0
  zone_id = data.aws_route53_zone.main[0].zone_id
  name    = local.vm_domain
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.vm[0].domain_name
    zone_id                = aws_cloudfront_distribution.vm[0].hosted_zone_id
    evaluate_target_health = false
  }
}
