# ---------------------------------------------------------------------------
# Solution 2 : machines virtuelles.
#   CloudFront -> ALB (2 AZ) -> Auto Scaling Group EC2 (2 AZ) -> RDS PostgreSQL
# Solution 3 (hybride) : mêmes ALB, EC2 et instance RDS. Un 2e service (port 8081,
# base shop_hybrid) reçoit POST /api/orders, routé par l'en-tête X-Shop: hybrid.
# Les EC2 sont dans des sous-réseaux publics (pas de NAT Gateway payante) mais
# n'acceptent que le trafic de l'ALB ; l'ALB n'accepte que CloudFront.
# Accès shell : AWS Systems Manager Session Manager (aucun port SSH ouvert).
# ---------------------------------------------------------------------------

data "aws_ec2_managed_prefix_list" "cloudfront" {
  count = local.ec2_enabled ? 1 : 0
  name  = "com.amazonaws.global.cloudfront.origin-facing"
}

locals {
  vm_arch = can(regex("^[a-z]+[0-9]+g", var.vm_instance_type)) ? "arm64" : "x86_64"
  # Files lues par les workers EC2 (aucune si seule la solution hybride est déployée).
  ec2_queue_arns = [for k in ["vm-processing", "kafka-bridge"] : aws_sqs_queue.main[k].arn if contains(keys(local.queues), k)]
}

data "aws_ssm_parameter" "al2023" {
  count = local.ec2_enabled ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-${local.vm_arch}"
}

resource "aws_security_group" "alb" {
  count       = local.ec2_enabled ? 1 : 0
  name        = "${local.name}-alb"
  description = "ALB : HTTP depuis CloudFront uniquement"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTP depuis les serveurs CloudFront"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront[0].id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
}

resource "aws_security_group" "app" {
  count       = local.ec2_enabled ? 1 : 0
  name        = "${local.name}-app"
  description = "Instances applicatives : ports 8080-8081 depuis le load balancer uniquement"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Applications (VM 8080, hybride 8081) depuis le load balancer"
    from_port       = 8080
    to_port         = 8081
    protocol        = "tcp"
    security_groups = [aws_security_group.alb[0].id]
  }

  egress {
    description = "Sortie (paquets, API AWS, RDS, MSK)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "app" {
  count              = local.ec2_enabled ? 1 : 0
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb[0].id]
  subnets            = aws_subnet.public[*].id
  idle_timeout       = 30
}

resource "aws_lb_target_group" "app" {
  count                = var.deploy_vm ? 1 : 0
  name                 = "${local.name}-app"
  port                 = 8080
  protocol             = "HTTP"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 10

  health_check {
    path                = "/api/health"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  count             = local.ec2_enabled ? 1 : 0
  load_balancer_arn = aws_lb.app[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Acces direct interdit"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "from_cloudfront" {
  count        = var.deploy_vm ? 1 : 0
  listener_arn = aws_lb_listener.http[0].arn
  priority     = 10

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_secret.result]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app[0].arn
  }
}

# Solution hybride : évaluée avant la règle VM, elle exige l'en-tête secret ET
# X-Shop: hybrid (ajoutés par la distribution CloudFront hybride).
resource "aws_lb_target_group" "hybrid" {
  count                = var.deploy_hybrid ? 1 : 0
  name                 = "${local.name}-hybrid"
  port                 = 8081
  protocol             = "HTTP"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 10

  health_check {
    path                = "/api/health"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener_rule" "hybrid" {
  count        = var.deploy_hybrid ? 1 : 0
  listener_arn = aws_lb_listener.http[0].arn
  priority     = 5

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_secret.result]
    }
  }

  condition {
    http_header {
      http_header_name = "X-Shop"
      values           = ["hybrid"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.hybrid[0].arn
  }
}

# ---------------------------------------------------------------------------
# IAM des instances
# ---------------------------------------------------------------------------

resource "aws_iam_role" "app" {
  count = local.ec2_enabled ? 1 : 0
  name  = "${local.name}-ec2-app"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "app_ssm" {
  count      = local.ec2_enabled ? 1 : 0
  role       = aws_iam_role.app[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "app" {
  count = local.ec2_enabled ? 1 : 0
  name  = "app"
  role  = aws_iam_role.app[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect   = "Allow"
          Action   = ["s3:GetObject"]
          Resource = ["${aws_s3_bucket.web.arn}/web/*", "${aws_s3_bucket.web.arn}/vm-app/*"]
        },
        {
          Effect    = "Allow"
          Action    = ["s3:ListBucket"]
          Resource  = [aws_s3_bucket.web.arn]
          Condition = { StringLike = { "s3:prefix" = ["web/*", "vm-app/*"] } }
        },
        { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [aws_ssm_parameter.db_password[0].arn] },
        { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
      ],
      length(local.ec2_queue_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = local.ec2_queue_arns
      }] : [],
      local.kms_statements,
    )
  })
}

resource "aws_iam_instance_profile" "app" {
  count = local.ec2_enabled ? 1 : 0
  name  = "${local.name}-ec2-app"
  role  = aws_iam_role.app[0].name
}

# ---------------------------------------------------------------------------
# Launch template + Auto Scaling Group
# ---------------------------------------------------------------------------

resource "aws_launch_template" "app" {
  count                  = local.ec2_enabled ? 1 : 0
  name_prefix            = "${local.name}-app-"
  image_id               = data.aws_ssm_parameter.al2023[0].insecure_value
  instance_type          = var.vm_instance_type
  vpc_security_group_ids = [aws_security_group.app[0].id]
  update_default_version = true

  iam_instance_profile {
    arn = aws_iam_instance_profile.app[0].arn
  }

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  user_data = base64encode(templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region            = var.region
    bucket            = aws_s3_bucket.web.id
    app_version       = md5(join("-", [for o in concat(values(aws_s3_object.vm_app), aws_s3_object.vm_products, values(aws_s3_object.frontend)) : o.etag]))
    db_host           = aws_db_instance.main[0].address
    db_user           = aws_db_instance.main[0].username
    db_password_param = aws_ssm_parameter.db_password[0].name
    topic_arn         = aws_sns_topic.orders.arn
    services          = local.ec2_services
    kafka_bootstrap   = var.enable_msk ? aws_msk_cluster.main[0].bootstrap_brokers : ""
    kafka_queue_url   = var.enable_msk ? aws_sqs_queue.main["kafka-bridge"].url : ""
  }))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-app" }
  }

  # Le front est téléchargé au démarrage : on attend qu'il soit dans S3.
  depends_on = [aws_s3_object.frontend]
}

resource "aws_autoscaling_group" "app" {
  count                     = local.ec2_enabled ? 1 : 0
  name                      = "${local.name}-app"
  min_size                  = var.vm_min_size
  max_size                  = var.vm_max_size
  vpc_zone_identifier       = aws_subnet.public[*].id
  target_group_arns         = concat(aws_lb_target_group.app[*].arn, aws_lb_target_group.hybrid[*].arn)
  health_check_type         = "ELB"
  health_check_grace_period = 240

  launch_template {
    id      = aws_launch_template.app[0].id
    version = aws_launch_template.app[0].latest_version
  }

  # Nouveau code ou nouvelle config => remplacement progressif des instances.
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 0
    }
  }

  tag {
    key                 = "Name"
    value               = "${local.name}-app"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_policy" "cpu" {
  count                  = local.ec2_enabled ? 1 : 0
  name                   = "cpu-target-60"
  autoscaling_group_name = aws_autoscaling_group.app[0].name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    target_value = 60
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
  }
}
