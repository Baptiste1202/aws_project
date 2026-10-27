variable "project" {
  description = "Nom du projet, utilisé comme préfixe de toutes les ressources."
  type        = string
  default     = "threadly"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project))
    error_message = "Minuscules, chiffres et tirets uniquement (3 à 21 caractères)."
  }
}

variable "environment" {
  description = "Nom de l'environnement (tag)."
  type        = string
  default     = "dev"
}

variable "region" {
  description = "Région AWS principale (Paris)."
  type        = string
  default     = "eu-west-3"
}

# ---------------------------------------------------------------------------
# Choix des solutions déployées
# ---------------------------------------------------------------------------

variable "deploy_serverless" {
  description = "Solution 1 : CloudFront + S3 + Lambda + DynamoDB."
  type        = bool
  default     = true
}

variable "deploy_vm" {
  description = "Solution 2 : CloudFront + ALB + Auto Scaling EC2 + RDS PostgreSQL."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Options payantes (désactivées par défaut pour rester dans le Free Tier)
# ---------------------------------------------------------------------------

variable "enable_msk" {
  description = "Crée un cluster Amazon MSK (Kafka). PAYANT (~0,10 $/h, 20-30 min de création). Nécessite deploy_vm = true."
  type        = bool
  default     = false
}

variable "enable_kms_cmk" {
  description = "Utilise une clé KMS gérée par le client (1 $/mois) au lieu des clés gérées par AWS (gratuites)."
  type        = bool
  default     = false
}

variable "enable_cloudtrail" {
  description = "Crée un trail CloudTrail (le premier trail de gestion du compte est gratuit)."
  type        = bool
  default     = true
}

variable "route53_zone_name" {
  description = "Zone hébergée Route 53 EXISTANTE (ex : mondomaine.fr). Vide = pas de nom de domaine personnalisé."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

variable "notification_email" {
  description = "Adresse qui reçoit les confirmations de commande, les alarmes et l'alerte budget (il faut confirmer l'abonnement SNS reçu par mail). Vide = pas d'e-mail."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Seuil du budget mensuel (alerte e-mail)."
  type        = number
  default     = 1
}

# ---------------------------------------------------------------------------
# Dimensionnement
# ---------------------------------------------------------------------------

variable "vm_instance_type" {
  description = "Type d'instance EC2 (t3.micro est éligible Free Tier à Paris)."
  type        = string
  default     = "t3.micro"
}

variable "vm_min_size" {
  description = "Nombre minimal d'instances de l'Auto Scaling Group. Mettre 2 pour une vraie haute disponibilité (hors Free Tier)."
  type        = number
  default     = 1
}

variable "vm_max_size" {
  description = "Nombre maximal d'instances de l'Auto Scaling Group."
  type        = number
  default     = 2
}

variable "db_instance_class" {
  description = "Classe RDS (db.t3.micro / db.t4g.micro éligibles Free Tier)."
  type        = string
  default     = "db.t3.micro"
}

variable "db_multi_az" {
  description = "RDS Multi-AZ (haute disponibilité de la base, PAYANT)."
  type        = bool
  default     = false
}

variable "msk_instance_type" {
  description = "Type de broker MSK."
  type        = string
  default     = "kafka.t3.small"
}

variable "msk_kafka_version" {
  description = "Version de Kafka pour MSK."
  type        = string
  default     = "3.6.0"
}

variable "log_retention_days" {
  description = "Rétention des logs CloudWatch."
  type        = number
  default     = 3
}

variable "fulfillment_delay_seconds" {
  description = "Délai simulé entre chaque étape de traitement (préparation, expédition)."
  type        = number
  default     = 5
}
