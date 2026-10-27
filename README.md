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

```bash
docker compose -f local/docker-compose.yml up --build -d   # environ 1 min au premier lancement
docker compose -f local/docker-compose.yml down -v         # arrêter et tout effacer
```

Les trois solutions tournent alors en local, sans compte AWS et sans coût :

| Port | Solution | Ce qui tourne |
|---|---|---|
| http://localhost:8080 | VM | [src/vm/app.py](src/vm/app.py) (comme sur EC2), PostgreSQL à la place de RDS |
| http://localhost:8081 | serverless | nginx (rôle de CloudFront) → Lambda `api` → DynamoDB |
| http://localhost:8082 | hybride | nginx avec le routage par chemin de la distribution hybride : `POST /api/orders` → `app.py` (base `shop_hybrid`), le reste de `/api/*` → Lambda `hybrid-read` |

- **[LocalStack](https://www.localstack.cloud/)** (édition communautaire, gratuite) simule
  Lambda, DynamoDB, SNS, SQS, S3 et SSM. [local/init_aws.py](local/init_aws.py) y crée ce
  que crée Terraform : topic, files et filtres, tables, bucket d'audit, et les six
  Lambdas avec **le code de `src/lambda/` tel quel**.
- **[local/nginx.conf](local/nginx.conf)** reproduit les deux distributions CloudFront.
- **LocalStack est épinglé sur 4.14.0**, la dernière version communautaire qui démarre
  sans jeton. Les versions récentes exigent un compte LocalStack gratuit :
  `LOCALSTACK_VERSION=<version> LOCALSTACK_AUTH_TOKEN=<jeton> docker compose ...`.
- **Docker doit être accessible** par LocalStack (`/var/run/docker.sock`) : chaque
  Lambda tourne dans un conteneur qu'il lance.
- **Quota Lambda :** le local applique le même que le compte AWS, 10 exécutions
  simultanées. Au-delà, LocalStack renvoie un 500 de type `TooManyRequestsException`, là
  où AWS renvoie un 429 ; le script de test compte les deux comme « limités par Lambda ».
- **Après une modification du code**, relancer avec `--build`.

> Le stock et les commandes sont conservés tant que la stack tourne. `down -v` puis `up`
> repart du catalogue initial de [data/products.json](data/products.json).

## Scénarios de test

[scripts/test_scenario.sh](scripts/test_scenario.sh) joue les scénarios de test via l'API
et affiche ✅ / ❌ pour chacun, puis un résumé. Le code de sortie vaut 1 si un scénario échoue.
Prérequis : `curl` et `python3`, plus l'AWS CLI en mode AWS.

```bash
# En local (stack Docker démarrée, voir ci-dessus) : un port par solution
./scripts/test_scenario.sh http://localhost:8080   # VM (port par défaut)
./scripts/test_scenario.sh http://localhost:8081   # serverless
./scripts/test_scenario.sh http://localhost:8082   # hybride

# Sur AWS, après ./up.sh : passer l'URL de l'une des solutions
./scripts/test_scenario.sh "$(terraform -chdir=infra output -raw serverless_url)"
./scripts/test_scenario.sh "$(terraform -chdir=infra output -raw vm_url)"
./scripts/test_scenario.sh "$(terraform -chdir=infra output -raw hybrid_url)"
```

| # | Scénario | Ce qu'il démontre |
|---|---|---|
| 1 | Afficher le catalogue | Le front et l'API lisent la base (DynamoDB ou RDS) via CloudFront |
| 2 | Commande de 2 produits suivie jusqu'à `SHIPPED` | Toute la chaîne événementielle : API → SNS → SQS (délai = emballage) → traitement → `PACKED` → `SHIPPED`, avec transporteur, n° de suivi et stock décrémenté |
| 3 | Commande supérieure au stock | Refus `409`, sans aucune modification du stock |
| 4 | Commandes simultanées dépassant le stock | Le décrément est atomique (transaction DynamoDB / `SELECT … FOR UPDATE`) : le stock final vaut exactement `stock initial − commandes acceptées`, sans jamais passer sous 0, et chaque requête reçoit `201` ou `409`. Les `429` (requête refusée par Lambda, quota de concurrence du compte atteint) sont comptés à part, signalés par ⚠️, sans faire échouer le scénario |
| 5 | Fichiers d'audit S3 | Les 3 événements de la commande du scénario 2 sont archivés par la Lambda audit dans `s3://<audit_bucket>/events/` |

Sur AWS, le bucket d'audit est lu dans `terraform output`. En local, le script lit le
bucket `threadly-local-audit` dans LocalStack (http://localhost:4566), et ignore le
scénario 5 si LocalStack ne répond pas. `AUDIT_BUCKET=...` force un autre bucket.
`TIMEOUT=120 ./scripts/test_scenario.sh` allonge l'attente du statut `SHIPPED` (90 s par défaut).
Pour la solution hybride, le stock affiché rattrape RDS de façon asynchrone : le script le
relit jusqu'à la valeur attendue (20 s au plus) avant de conclure.

Le scénario 4 vide le stock d'un produit (celui qui en a le moins). Pour repartir du
catalogue initial : `docker compose -f local/docker-compose.yml down -v` puis `up` en local,
ou `./up.sh` sur AWS.

## Les trois solutions

Les trois solutions sont déployées en parallèle par défaut (`deploy_serverless`, `deploy_vm`,
`deploy_hybrid` dans `terraform.tfvars`) et servent **le même front**
([src/frontend](src/frontend)) qui appelle `/api/*`. Chacune a sa distribution CloudFront.

| | Solution 1 : serverless | Solution 2 : VM | Solution 3 : hybride |
|---|---|---|---|
| Front | S3 (OAC) | servi par les EC2 | S3 (OAC) |
| Catalogue et suivi | Lambda `api` → DynamoDB | EC2 → RDS PostgreSQL | Lambda `hybrid-read` → DynamoDB |
| Commandes | Lambda `api` → transaction DynamoDB | EC2 → transaction SQL (RDS) | EC2 → transaction SQL (RDS) |
| Traitement interne | Lambda `processor` | worker sur les EC2 | Lambda `hybrid-processor` (+ `hybrid-projector`) |
| Terraform | `lambda.tf`, `database.tf`, `cdn.tf` | `vm.tf`, `database.tf`, `cdn.tf` | `hybrid.tf` + ressources partagées |

```
                     ┌──────────── Solution 1 : Serverless ────────────┐
Navigateur ─HTTPS─► CloudFront ─┬─ /*     ─► S3 (site statique, OAC)
                                └─ /api/* ─► Lambda "api" (Function URL) ─► DynamoDB
                                                       │
                     ┌──────────── Solution 2 : VM ────┼────────────────┐
Navigateur ─HTTPS─► CloudFront ─► ALB (2 AZ) ─► ASG EC2 (2 AZ) ─► RDS PostgreSQL (base shop)
                                                       │
                     ┌──────────── Solution 3 : Hybride┼────────────────┐
Navigateur ─HTTPS─► CloudFront ─┬─ /*                ─► S3 (même bucket, OAC)
                                ├─ POST /api/orders  ─► ALB ─► EC2 :8081 ─► RDS (base shop_hybrid)
                                └─ /api/* (lectures) ─► Lambda "hybrid-read" ─► DynamoDB hybrid-*
                                                       │
                                     publish           ▼
                              ┌──────────── SNS "order-events" ────────────┐
                              │ filtre source/event_type (pub/sub)          │
     e-mail confirmation ◄────┤                                             │
     SQS processing     ◄─────┤ ─► Lambda processor  (serverless)           │
     SQS vm-processing  ◄─────┤ ─► worker sur EC2    (VM)                   │
     SQS hybrid-projection ◄──┤ ─► Lambda hybrid-projector ─► DynamoDB      │
     SQS hybrid-processing ◄──┤ ─► Lambda hybrid-processor  (hybride)       │
     SQS audit          ◄─────┤ ─► Lambda audit ─► S3 (archive, Athena)     │
     SQS kafka-bridge   ◄─────┘ ─► EC2 ─► MSK Kafka ─► stats temps réel     │ (option)
```

Cycle de vie d'une commande (identique dans les trois solutions) :

1. `POST /api/orders` : contrôle et décrément du stock **atomique** (transaction
   DynamoDB / `SELECT … FOR UPDATE` PostgreSQL), statut `CONFIRMED`, événement
   `ORDER_CREATED` publié sur SNS → e-mail de confirmation.
2. Le traitement interne reçoit l'événement via SQS (délai de 5 s = emballage) →
   `PACKED`, publie `ORDER_PACKED`.
3. Même chose pour l'expédition → `SHIPPED` + transporteur et n° de suivi, publie
   `ORDER_SHIPPED` → e-mail d'expédition.
4. Chaque événement est archivé dans le bucket d'audit (`events/event_type=…/date=…/`).

Le front affiche l'avancement en temps réel (rafraîchi toutes les 2 s).
Les mises à jour sont conditionnelles, donc une transition n'est jamais appliquée deux fois.
Si un message est rejoué alors que le statut est déjà à jour (par exemple après un échec de
publication SNS), l'événement suivant est republié pour que la commande ne reste pas bloquée.
Après 3 échecs un message part en Dead Letter Queue, ce qui déclenche une alarme.

### Solution 3 : hybride, routage et cohérence des données

Chaque partie est placée sur le service qui lui convient. La distribution CloudFront
route par chemin ([infra/hybrid.tf](infra/hybrid.tf)) :

| Chemin | Destination | Pourquoi |
|---|---|---|
| `/*` | S3 (même bucket et OAC que la solution serverless) | Fichiers statiques : rien à calculer |
| `/api/products*`, `/api/orders/{id}` | Lambda `hybrid-read` → DynamoDB | Lectures nombreuses et simples, sans serveur |
| `POST /api/orders` | ALB → EC2 (service `shop-hybrid`, port 8081) → RDS (base `shop_hybrid`) | Transaction SQL, sans limite de concurrence Lambda |

**RDS fait foi pour le stock et la création des commandes ; DynamoDB fait foi pour le statut
des commandes** (CQRS, cohérence à terme). Chaque `ORDER_CREATED` porte le nouveau stock et
sa version : la Lambda `hybrid-projector` recopie la commande et le stock dans DynamoDB (une
version plus ancienne est ignorée), puis `hybrid-processor` (code du processor serverless)
fait avancer le statut. Le VPC, l'ALB, les EC2, l'instance RDS, le bucket web, SNS et l'audit
sont partagés avec les solutions 1 et 2 ; la règle ALB hybride exige l'en-tête secret
**et** `X-Shop: hybrid`.

### Haute disponibilité et résilience

| | Serverless | VM | Hybride |
|---|---|---|---|
| Calcul | Lambda (multi-AZ natif) | ASG sur 2 AZ, health check ALB, remplacement auto | Lambda pour les lectures et le traitement, ASG sur 2 AZ pour les commandes |
| Données | DynamoDB (multi-AZ natif) | RDS Single-AZ (Multi-AZ = `db_multi_az`, payant) | RDS Single-AZ pour les écritures, DynamoDB multi-AZ pour les lectures |
| Entrée | CloudFront | CloudFront + ALB sur 2 AZ | CloudFront + ALB sur 2 AZ |
| Découplage | SNS → SQS + DLQ | SNS → SQS + DLQ | SNS → SQS + DLQ, projection versionnée |

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
scripts/test_scenario.sh   scénarios de test (local ou AWS)
scripts/cleanup_orphans.sh ressources "threadly-dev" absentes de l'état Terraform (simulation par défaut)
docs/                      rapport (rapport.md / .pdf), schémas d'architecture, captures
data/products.json         la "fausse base" : 12 produits (4 t-shirts, 4 pulls, 4 baskets)
src/frontend/              site (HTML/CSS/JS sans dépendance), commun aux 3 solutions
src/lambda/{api,processor,audit,projector}/handler.py
src/vm/app.py              appli EC2 (VM et hybride) : site + API + worker SQS (+ Kafka)
infra/                     Terraform
  network.tf               VPC 2 AZ, sous-réseaux publics/privés, endpoints S3/DynamoDB
  database.tf              DynamoDB + seed, RDS PostgreSQL
  storage.tf               buckets web et audit
  messaging.tf             SNS, SQS + DLQ, abonnements filtrés
  lambda.tf                Lambdas, IAM, Function URL, déclencheurs SQS
  vm.tf                    ALB, Auto Scaling Group, launch template, IAM
  cdn.tf / dns.tf          CloudFront, Route 53 + ACM (optionnel)
  hybrid.tf                CloudFront de la solution hybride (routage par chemin)
  msk.tf                   Kafka (optionnel)
  security.tf              KMS, CloudTrail
  monitoring.tf            alarmes, dashboard, budget
```
