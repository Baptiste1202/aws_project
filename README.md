# Threadly — boutique de vêtements sur AWS (Paris, Free Tier)

Boutique en ligne fictive (t-shirts, pulls, baskets) : catalogue, commande d'un ou
plusieurs produits, confirmation et suivi en temps réel, audit et traitement interne
(emballage, expédition simulés). Toute l'infrastructure est décrite en Terraform,
région **eu-west-3 (Paris)**, et se crée / se supprime en **une commande**.

## Démarrage rapide

```powershell
# (une seule fois) adapter la config
copy infra\terraform.tfvars.example infra\terraform.tfvars   # puis mettre votre e-mail

.\up.ps1     # crée tout (~10-15 min, surtout RDS et CloudFront) et affiche les URLs
.\down.ps1   # supprime tout, plus aucun coût (~10 min)
```

Sous Linux/macOS/Git Bash : `./up.sh` et `./down.sh`.

Prérequis : Terraform ≥ 1.6, des identifiants AWS configurés (`aws configure`).
Après chaque `up`, AWS envoie un mail « Subscription Confirmation » à confirmer pour
recevoir les confirmations de commande et les alarmes.

### En local, sans AWS (Docker)

```powershell
docker compose -f local/docker-compose.yml up --build   # puis http://localhost:8080
docker compose -f local/docker-compose.yml down         # arrêter
```

Cette commande lance l'application de la solution VM ([src/vm/app.py](src/vm/app.py)) avec un vrai PostgreSQL.
SNS, SQS et SSM sont simulés par [moto](https://github.com/getmoto/moto), et
[local/init_aws.py](local/init_aws.py) y crée le topic et les files, comme le fait Terraform.
Aucun appel au vrai AWS, aucun coût. Après une modification du code, relancer avec `--build`.

> Rien n'est conservé entre deux `up` : le catalogue est rechargé depuis
> [data/products.json](data/products.json) et les commandes repartent de zéro.

## Les deux solutions

Les deux sont déployées en parallèle (désactivables avec `deploy_serverless` /
`deploy_vm`) et servent **le même front** ([src/frontend](src/frontend)) qui appelle `/api/*`.

```
                     ┌──────────── Solution 1 : Serverless ────────────┐
Navigateur ─HTTPS─► CloudFront ─┬─ /*     ─► S3 (site statique, OAC)
                                └─ /api/* ─► Lambda "api" (Function URL) ─► DynamoDB
                                                       │
                     ┌──────────── Solution 2 : VM ────┼────────────────┐
Navigateur ─HTTPS─► CloudFront ─► ALB (2 AZ) ─► ASG EC2 (2 AZ) ─► RDS PostgreSQL
                                                       │
                                     publish           ▼
                              ┌──────────── SNS "order-events" ────────────┐
                              │ filtre source/event_type (pub/sub)          │
     e-mail confirmation ◄────┤                                             │
     SQS processing     ◄─────┤ ─► Lambda processor  (serverless)           │
     SQS vm-processing  ◄─────┤ ─► worker sur EC2    (VM)                   │
     SQS audit          ◄─────┤ ─► Lambda audit ─► S3 (archive, Athena)     │
     SQS kafka-bridge   ◄─────┘ ─► EC2 ─► MSK Kafka ─► stats temps réel     │ (option)
```

Cycle de vie d'une commande (identique dans les deux solutions) :

1. `POST /api/orders` : contrôle et décrément du stock **atomique** (transaction
   DynamoDB / `SELECT … FOR UPDATE` PostgreSQL), statut `CONFIRMED`, événement
   `ORDER_CREATED` publié sur SNS → e-mail de confirmation.
2. Le traitement interne reçoit l'événement via SQS (délai de 5 s = emballage) →
   `PACKED`, publie `ORDER_PACKED`.
3. Même chose pour l'expédition → `SHIPPED` + transporteur et n° de suivi, publie
   `ORDER_SHIPPED` → e-mail d'expédition.
4. Chaque événement est archivé dans le bucket d'audit (`events/event_type=…/date=…/`).

Le front affiche l'avancement en temps réel (rafraîchi toutes les 2 s).
Les mises à jour sont conditionnelles, donc un message SQS livré deux fois est sans effet.
Après 3 échecs un message part en Dead Letter Queue, ce qui déclenche une alarme.

### Haute disponibilité et résilience

| | Serverless | VM |
|---|---|---|
| Calcul | Lambda (multi-AZ natif) | ASG sur 2 AZ, health check ALB, remplacement auto |
| Données | DynamoDB (multi-AZ natif) | RDS Single-AZ (Multi-AZ = `db_multi_az`, payant) |
| Entrée | CloudFront | CloudFront + ALB sur 2 AZ |
| Découplage | SNS → SQS + DLQ | SNS → SQS + DLQ |

## Adaptations Free Tier (fonctions éliminées ou désactivées)

| Élément | Choix par défaut | Pour l'activer |
|---|---|---|
| **MSK (Kafka)** | désactivé, aucun Free Tier (~0,10 $/h, 20-30 min de création) | `enable_msk = true` |
| KMS clé client | clés gérées AWS (gratuites) | `enable_kms_cmk = true` (1 $/mois) |
| RDS Multi-AZ | Single-AZ db.t3.micro, 20 Go, sans sauvegarde | `db_multi_az = true` |
| 2e instance EC2 | `vm_min_size = 1` (750 h/mois gratuites) | `vm_min_size = 2` |
| NAT Gateway | aucune : EC2 en sous-réseau public, protégées par security groups | — |
| API Gateway | remplacée par une Lambda Function URL (gratuite) | — |
| Route 53 | pas de domaine, URLs `*.cloudfront.net` | `route53_zone_name = "mondomaine.fr"` |
| DynamoDB | capacité provisionnée 5/5 (Free Tier permanent : 25 RCU/WCU) | — |
| Logs | rétention 3 jours | `log_retention_days` |

Ce qui peut quand même coûter quelques centimes, même avec le `down` chaque soir :
- l'**ALB** et les **IPv4 publiques** (gratuits seulement pendant les 12 mois du Free Tier « historique ») ;
- **CloudTrail** si le compte a déjà un autre trail (`enable_cloudtrail = false` sinon).

Le budget AWS (créé si `notification_email` est renseigné) envoie un mail dès que
la prévision dépasse 80 % de `monthly_budget_usd`. Dans **Cost Explorer**, on peut
filtrer par le tag `Project = threadly`, posé sur toutes les ressources.

## Sécurité

- **IAM** : un rôle par Lambda, et un pour les EC2, chacun avec le strict minimum (moindre privilège).
- Les origines n'acceptent que CloudFront. L'ALB est limité à la *prefix list* CloudFront et vérifie l'en-tête secret `X-Origin-Verify`, que la Lambda vérifie aussi.
- Les buckets S3 sont privés : le site est servi via Origin Access Control.
- RDS est dans des sous-réseaux privés, accessible uniquement depuis les EC2. Son mot de passe est généré aléatoirement et stocké dans SSM Parameter Store (SecureString).
- Pas de SSH : on accède aux EC2 par **Session Manager**.
- Les données sont chiffrées au repos (DynamoDB, RDS, SQS, SNS, S3), et IMDSv2 est obligatoire sur les EC2.
- **CloudTrail** journalise toutes les actions faites sur le compte.

## Observabilité

`terraform output dashboard_url` ouvre le tableau de bord CloudWatch, qui montre :
les commandes publiées, les files SQS, les invocations et erreurs Lambda, l'ALB et le CPU EC2/RDS.

Alarmes (envoyées par e-mail) : DLQ non vide, erreurs Lambda, aucune instance saine derrière l'ALB.

Logs :
- **Lambda** : `/aws/lambda/threadly-dev-*`
- **EC2** : se connecter par Session Manager, puis `journalctl -u shop -f`

## Arborescence

```
up.ps1 / down.ps1          une commande pour tout créer / tout supprimer
data/products.json         la "fausse base" : 12 produits (4 t-shirts, 4 pulls, 4 baskets)
src/frontend/              site (HTML/CSS/JS sans dépendance), commun aux 2 solutions
src/lambda/{api,processor,audit}/handler.py
src/vm/app.py              appli EC2 : site + API + worker SQS (+ Kafka)
infra/                     Terraform
  network.tf               VPC 2 AZ, sous-réseaux publics/privés, endpoints S3/DynamoDB
  database.tf              DynamoDB + seed, RDS PostgreSQL
  storage.tf               buckets web et audit
  messaging.tf             SNS, SQS + DLQ, abonnements filtrés
  lambda.tf                Lambdas, IAM, Function URL, déclencheurs SQS
  vm.tf                    ALB, Auto Scaling Group, launch template, IAM
  cdn.tf / dns.tf          CloudFront, Route 53 + ACM (optionnel)
  msk.tf                   Kafka (optionnel)
  security.tf              KMS, CloudTrail
  monitoring.tf            alarmes, dashboard, budget
```
