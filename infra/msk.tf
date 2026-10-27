# ---------------------------------------------------------------------------
# Streaming Kafka avec Amazon MSK (OPTIONNEL : var.enable_msk).
# MSK n'a pas de Free Tier (~0,05 $/h par broker kafka.t3.small, 2 brokers
# minimum) et met 20-30 min à se créer : il est désactivé par défaut.
#
# Flux : SNS -> SQS "kafka-bridge" -> instances EC2 (producteur) -> topic
# "order-events" -> consommateur EC2 qui calcule des statistiques en temps
# réel exposées sur /api/stats (affichées dans le site).
# ---------------------------------------------------------------------------

resource "aws_security_group" "msk" {
  count       = var.enable_msk ? 1 : 0
  name        = "${local.name}-msk"
  description = "Brokers Kafka, accessibles depuis les instances applicatives"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Kafka (plaintext, intra-VPC)"
    from_port       = 9092
    to_port         = 9092
    protocol        = "tcp"
    security_groups = [aws_security_group.app[0].id]
  }
}

resource "aws_msk_configuration" "main" {
  count          = var.enable_msk ? 1 : 0
  name           = "${local.name}-kafka-${local.suffix}"
  kafka_versions = [var.msk_kafka_version]

  server_properties = <<-EOT
    auto.create.topics.enable=true
    default.replication.factor=2
    min.insync.replicas=1
    num.partitions=3
    log.retention.hours=24
  EOT
}

resource "aws_cloudwatch_log_group" "msk" {
  count             = var.enable_msk ? 1 : 0
  name              = "/aws/msk/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_msk_cluster" "main" {
  count                  = var.enable_msk ? 1 : 0
  cluster_name           = "${local.name}-kafka"
  kafka_version          = var.msk_kafka_version
  number_of_broker_nodes = length(aws_subnet.private)

  broker_node_group_info {
    instance_type   = var.msk_instance_type
    client_subnets  = aws_subnet.private[*].id
    security_groups = [aws_security_group.msk[0].id]

    storage_info {
      ebs_storage_info {
        volume_size = 10
      }
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.main[0].arn
    revision = aws_msk_configuration.main[0].latest_revision
  }

  client_authentication {
    unauthenticated = true
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = local.kms_key_arn
    encryption_in_transit {
      client_broker = "TLS_PLAINTEXT"
      in_cluster    = true
    }
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk[0].name
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.deploy_vm
      error_message = "enable_msk nécessite deploy_vm = true (les instances EC2 produisent et consomment les événements Kafka)."
    }
  }
}
