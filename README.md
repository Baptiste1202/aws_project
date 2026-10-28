# Threadly — boutique de vêtements sur AWS (Paris, Free Tier)

Boutique en ligne fictive (t-shirts, pulls, baskets), réalisée en **trois solutions**
— serverless, machines virtuelles et hybride — qui partagent le même code métier et le
même front, mais pas la même infrastructure :

- **Clients** : catalogue avec recherche et filtres, comptes (Amazon Cognito), panier
  avec réservation du stock, paiement par carte (Stripe en mode test), frais de port,
  suivi en temps réel jusqu'à la livraison, facture PDF, e-mails (Amazon SES), avis,
  liste de souhaits, retours sous 14 jours remboursés.
- **Boutique** : stock par entrepôt (Paris, Lyon), back-office (produits, mouvements,
  codes promo, images, import et export CSV), réapprovisionnement fournisseur
  automatique, tableau de bord, rapport quotidien, audit de tous les événements
  requêtable par Athena, traces X-Ray.
- **Qualité** : 36 scénarios de test joués sur les trois solutions, en local (Docker)
  comme sur AWS, test de charge k6, intégration continue (GitHub Actions).

Toute l'infrastructure est décrite en Terraform, région **eu-west-3 (Paris)**, et se
crée / se supprime en **une commande**. Le rapport ([docs/rapport.pdf](docs/rapport.pdf))
compare les trois solutions.

## Démarrage rapide

```bash
# (une seule fois) adapter la config
cp infra/terraform.tfvars.example infra/terraform.tfvars   # puis mettre votre e-mail

./up.sh     # crée tout (~10-15 min, surtout RDS et CloudFront) et affiche les URLs
./down.sh   # supprime tout, plus aucun coût (~10 min)
```

Sous Windows : Git Bash ou WSL.

Prérequis : Terraform ≥ 1.9, des identifiants AWS configurés (`aws configure`).
Après chaque `up`, AWS envoie à `notification_email` la confirmation de l'abonnement
SNS (alarmes, budget) : cliquer sur « Confirm subscription ».

**Vérification SES (étape unique, avant le premier `up`)** : les e-mails aux clients
(confirmation avec facture, expédition, livraison, annulation, remboursement, stock bas)
partent par Amazon SES depuis `notification_email`. Cette adresse doit être une identité
SES vérifiée dans la région. Elle est créée **une seule fois, hors Terraform** :
`down.sh` ne la supprime pas, `up.sh` ne renvoie donc pas d'e-mail de vérification.

```bash
aws sesv2 create-email-identity --region eu-west-3 --email-identity vous@example.com
# cliquer sur le lien de l'e-mail « Amazon Web Services – Email Address Verification Request »
aws sesv2 get-email-identity --region eu-west-3 --email-identity vous@example.com \
  --query VerifiedForSendingStatus   # doit afficher true
```

`up.sh` s'arrête avant l'apply, avec la marche à suivre, si l'identité est absente ou
non vérifiée ([scripts/check_ses.sh](scripts/check_ses.sh)). Terraform la lit sans la
gérer (bloc `check` de [payments.tf](infra/payments.tf)) et ne fait alors qu'avertir,
pour ne jamais bloquer un `destroy`. Le compte étant en « sandbox » SES, tous les e-mails
sont redirigés vers cette adresse vérifiée, avec le destinataire prévu en tête de message.
Sans e-mail : `notification_email = ""`. Après les tests, l'identité peut être supprimée
à la main (`aws sesv2 delete-email-identity`) ; elle ne coûte rien.

**Paiement** : variable `payment_mode` dans `infra/terraform.tfvars` (ignoré par git,
comme `terraform.tfstate`).

- `payment_mode = "stripe"` (par défaut) : paiement par carte avec Stripe en **mode test**.
  Ajouter les clés du tableau de bord Stripe, mode « Test » (seules `pk_test_` et
  `sk_test_` sont acceptées). Sans elles, `terraform plan` échoue avec un message clair.
  La clé secrète est stockée dans SSM Parameter Store (SecureString), lue à l'exécution.
- `payment_mode = "simulated"` : **sans compte Stripe**, chaque commande est confirmée
  aussitôt (aucune clé à fournir). Le scénario 23 est alors sans objet.

```hcl
payment_mode           = "stripe"        # ou "simulated"
stripe_publishable_key = "pk_test_..."
stripe_secret_key      = "sk_test_..."
```

### Démonstration : compte admin créé par `up.sh`

Pour accéder au back-office (`/admin.html`) juste après le déploiement, renseigner
`demo_admin_email` dans `infra/terraform.tfvars` :

```hcl
demo_admin_email = "admin@example.com"   # vide (par défaut) = aucun compte créé
```

À la fin de `./up.sh`, [scripts/create_demo_admin.sh](scripts/create_demo_admin.sh) crée cet
utilisateur dans le pool Cognito déployé (e-mail marqué vérifié, sans e-mail d'invitation),
l'ajoute au groupe `admin` et affiche un mot de passe aléatoire **une seule fois** :

```text
Compte admin de démonstration (back-office : /admin.html)
  identifiant  : admin@example.com
  mot de passe : …
```

Le mot de passe n'est écrit ni dans un fichier ni dans l'état Terraform : l'utilisateur est
créé par l'AWS CLI, pas par Terraform. Pour en obtenir un nouveau, relancer `./up.sh` (ou
seulement `./scripts/create_demo_admin.sh`) : le compte existant reçoit un nouveau mot de
passe. `./down.sh` supprime le pool, donc le compte.

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
| http://localhost:8082 | hybride | nginx avec le routage par méthode HTTP de la distribution hybride : écritures sur `/api/*` (`POST`, `PUT`, `DELETE`) → `app.py` (base `shop_hybrid`), lectures (`GET`, `HEAD`) → Lambda `hybrid-read` |
| http://localhost:12111 | paiement | faux Stripe local ([local/stripe_local.py](local/stripe_local.py)) : idempotence, cartes de test `pm_card_visa` / `pm_card_chargeDeclined`, annulation et remboursement |
| http://localhost:3000 | Grafana | tableau de bord « Threadly : ventes et stock » provisionné automatiquement (bases PostgreSQL de la VM et de l'hybride, compte en lecture seule ; consultation anonyme) |
| http://localhost:9100 | identité | fournisseur d'identité local (à la place de Cognito) ; comptes de démonstration `admin@threadly.local` / `admin-local-pass`, `magasinier@threadly.local` / `mag-local-pass`, `client@threadly.local` / `client-local-pass` (sans valeur hors de la stack) |

- **[LocalStack](https://www.localstack.cloud/)** (édition communautaire, gratuite) simule
  Lambda, DynamoDB, SNS, SQS, S3, SSM, Step Functions et SES. [local/init_aws.py](local/init_aws.py)
  y crée ce que crée Terraform : topic, files et filtres, tables, buckets, machines d'état
  (même définition que [fulfillment.asl.json](infra/templates/fulfillment.asl.json)) et les
  Lambdas avec **le code de `src/lambda/` tel quel**. Les e-mails restent dans LocalStack
  (`http://localhost:4566/_aws/ses`).
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
| 2 | Commande de 2 produits payée, suivie jusqu'à `DELIVERED` | Paiement par carte de test, puis toute la chaîne : `PENDING_PAYMENT` → `CONFIRMED` → `PACKED` → `SHIPPED` → `IN_TRANSIT` → `DELIVERED` (Step Functions ou worker EC2), transporteur, n° de suivi, stock décrémenté |
| 3 | Commande supérieure au stock | Refus `409`, sans aucune modification du stock |
| 4 | Commandes simultanées dépassant le stock | Le décrément est atomique (transaction DynamoDB / `SELECT … FOR UPDATE`) : le stock final vaut exactement `stock initial − commandes acceptées`, sans jamais passer sous 0, et chaque requête reçoit `201` ou `409`. Les `429` (requête refusée par Lambda, quota de concurrence du compte atteint) sont comptés à part, signalés par ⚠️, sans faire échouer le scénario |
| 5 | Fichiers d'audit S3 | Les 6 événements de la commande du scénario 2 (`ORDER_CREATED` à `ORDER_DELIVERED`) sont archivés par la Lambda audit dans `s3://<audit_bucket>/events/` |
| 6 | Back-office | Routes admin refusées sans jeton (401) ; création, modification, archivage d'un produit (absent du catalogue, commande refusée) |
| 7 | Multi-entrepôts | Une commande de 4 unités est servie par Paris (3) puis Lyon (1) |
| 8 | Journal des mouvements | Réappro, casse, inventaire et vente dans l'ordre ; une casse supérieure au stock est refusée (409) |
| 9 | Annulation | Le stock est remis ; seconde annulation et annulation d'une commande expédiée refusées (409) |
| 10 | Réservations | Le stock réservé par un panier est refusé aux autres ; la commande du panier le consomme ; libération manuelle ; expiration (10 s) |
| 11 | Réservations simultanées | 9 paniers pour 6 unités : jamais plus de 6 réservées, disponible et réservé cohérents |
| 12 | Alerte stock bas | `STOCK_LOW` publié sur SNS une seule fois au franchissement du seuil, archivé dans l'audit S3 |
| 13 | CSV via S3 | Export par lien présigné, modification, dépôt par lien présigné, import (1 produit modifié, 1 créé) |
| 14 | Valorisation | Valeur = stock physique × prix de revient, par produit, par entrepôt et au total |
| 15 | Comptes et rôles | Client 403 sur l'admin ; magasinier : stock oui, catalogue et promos non ; 6 jetons forgés refusés (401), dont un jeton du fournisseur local ; `infra/` ne référence pas le fournisseur local |
| 16 | Recherche et filtres | Texte, catégorie, prix minimum et maximum, en stock, tris |
| 17 | Codes promo | Remise en % ou en €, limite d'utilisations, expiration ; 4 commandes simultanées sur un code à usage unique : 1 seule acceptée |
| 18 | Avis | Réservés aux clients ayant commandé le produit ; un avis par client ; note moyenne à jour |
| 19 | Liste de souhaits | Alerte unique quand un produit souhaité revient en stock |
| 20 | Mes commandes | Chaque client ne voit que ses commandes |
| 21 | Images produit | Formulaire de dépôt présigné réservé à l'admin (type imposé par S3), miniatures WebP 160 et 480 px par la Lambda thumbnailer, faux fichier ignoré, original non public |
| 22 | Temps réel (AWS) | WebSocket : statuts de `ORDER_PAID` à `ORDER_DELIVERED` poussés sans polling, alerte de retour en stock poussée au client connecté, jeton forgé refusé ; non applicable en local |
| 23 | Paiement par carte | 402 sans paiement, carte refusée (`card_declined`), paiement vérifié côté serveur (statut, montant, devise, commande), confirmation rejouée sans effet, `client_secret` jamais exposé par `GET` ; en local : un seul PaymentIntent par commande (clé d'idempotence) |
| 24 | Purge des impayés | Commande payée chez Stripe sans retour sur la boutique : confirmée par la purge ; commande jamais payée : PaymentIntent annulé chez Stripe puis commande annulée, stock rendu |
| 25 | Frais de port | Standard 4,90 € (offerte dès 60 € d'articles après remise), express 9,90 €, mode inconnu refusé |
| 26 | Workflow de livraison | 5 étapes jusqu'à `DELIVERED` (Step Functions en serverless et hybride, worker EC2 pour la VM) ; commande payée puis annulée : remboursée une fois, le workflow s'arrête |
| 27 | Facture PDF | Réservée au client de la commande (et à l'admin), une fois payée : anonyme 401, impayée 409, autre client 403 ; PDF valide par lien présigné |
| 28 | E-mails SES | Confirmation (facture jointe), expédition, livraison et alerte de retour en stock (liste de souhaits) envoyées ; LocalStack en local, journaux de la Lambda `mailer` sur AWS |
| 29 | Retour et remboursement | Commande livrée : retour demandé par son client (anonyme 401, autre client 403, motif inconnu 400, seconde demande 409), réceptionné par le magasinier : `RETURNED`, mouvement `RETURN`, stock remis, remboursée une fois (seconde réception 409), e-mails « retour enregistré » et « remboursement » |
| 30 | Retour refusé | Commande non livrée : 409 ; règle des 14 jours après la livraison (13 jours : accepté, 15 jours : refusé) |
| 31 | Réappro fournisseur | Deux passages sous le seuil : un seul bon de commande, livré après le délai fournisseur (mouvement `RESTOCK` réf. du bon), stock remonté une fois ; Step Functions en serverless, worker EC2 pour la VM et l'hybride |
| 32 | Tableau de bord | Back-office (admin, magasinier ; anonyme 401, client 403) : chiffre d'affaires du jour et des 7 jours, top ventes, ruptures et stocks bas, commandes par statut, mis à jour par une commande |
| 33 | Rapport quotidien | CSV (ventes 24 h, stock, valorisation) dans S3 et envoyé par SES, CSV joint ; à la demande (admin seulement) et par la cible d'EventBridge Scheduler (Lambda `reporter`, ou worker EC2 pour la VM) |
| 34 | Athena + Glue | Table `events` partitionnée par projection (event_type, date) ; 3 requêtes nommées : chiffre d'affaires par jour, délai commande → livraison, annulations (AWS seulement) |
| 35 | X-Ray | Traces des Lambdas et des machines Step Functions (AWS seulement) |
| 36 | Grafana (local) | Tableau de bord provisionné, source PostgreSQL en lecture seule : le chiffre d'affaires du jour lu par Grafana est celui du back-office (VM) |

Les scénarios 6 à 14 sont dans [scripts/scenarios_stock.py](scripts/scenarios_stock.py), 15 à 20
dans [scripts/scenarios_client.py](scripts/scenarios_client.py), 21 et 22 dans
[scripts/scenarios_media.py](scripts/scenarios_media.py), 23 à 28 dans
[scripts/scenarios_order.py](scripts/scenarios_order.py), 29 à 31 dans
[scripts/scenarios_returns.py](scripts/scenarios_returns.py) et 32 à 36 dans
[scripts/scenarios_pilotage.py](scripts/scenarios_pilotage.py) (Python, bibliothèque standard) ;
ils créent leurs propres produits et codes promo de test, avec un identifiant aléatoire unique à
chaque lancement ([scripts/testids.py](scripts/testids.py), par exemple `ZZ-482913065`) : la suite peut être
relancée sans réinitialiser les données. `SKIP_STOCK=1`, `SKIP_CLIENT=1`, `SKIP_MEDIA=1`,
`SKIP_ORDER=1`, `SKIP_RETURNS=1` et `SKIP_PILOTAGE=1` les ignorent. Les commandes de test sont payées par
[scripts/testpay.py](scripts/testpay.py), comme le ferait Stripe.js : clé publiable et
cartes de test seulement, jamais la clé secrète. Les
comptes de test sont créés par [scripts/testauth.py](scripts/testauth.py) : utilisateurs Cognito
temporaires sur AWS (mots de passe aléatoires jamais écrits, comptes supprimés en fin de test),
fournisseur d'identité local dans la stack Docker.

Sur AWS, le bucket d'audit est lu dans `terraform output`. En local, le script lit le
bucket `threadly-local-audit` dans LocalStack (http://localhost:4566), et ignore le
scénario 5 si LocalStack ne répond pas. `AUDIT_BUCKET=...` force un autre bucket.
`TIMEOUT=120 ./scripts/test_scenario.sh` allonge l'attente du statut `DELIVERED` (90 s par défaut).
Pour la solution hybride, le stock affiché rattrape RDS de façon asynchrone : le script le
relit jusqu'à la valeur attendue (20 s au plus) avant de conclure.

Le scénario 4 vide le stock d'un produit (celui qui en a le moins). Pour repartir du
catalogue initial : `docker compose -f local/docker-compose.yml down -v` puis `up` en local,
ou `./up.sh` sur AWS.

## API commune et back-office

Les trois solutions exposent la même API, définie une seule fois dans
[src/shop/api.py](src/shop/api.py) : le front, le back-office (`/admin.html`) et le script de
test sont partagés. Le code commun ([src/shop](src/shop)) a deux implémentations du stockage :
PostgreSQL ([sql_store.py](src/shop/sql_store.py), VM et écritures de l'hybride) et DynamoDB
([dynamo_store.py](src/shop/dynamo_store.py), serverless et lectures de l'hybride).

| Fonction | Routes |
|---|---|
| Catalogue, stock par entrepôt (Paris, Lyon) | `GET /api/products`, `GET /api/products/{id}` |
| Réservation du panier (15 min, purge des réservations expirées) | `POST` / `DELETE /api/carts/{panier}/reservations` |
| Commande (consomme les réservations du panier) et annulation tant que non expédiée | `POST /api/orders`, `GET /api/orders/{id}`, `POST /api/orders/{id}/cancel` |
| Produits (CRUD, archivage) | `GET`, `POST /api/admin/products`, `PUT`, `DELETE /api/admin/products/{id}` |
| Mouvements (réappro, casse, inventaire) et journal | `GET`, `POST /api/admin/stock/movements` |
| Stocks bas, valorisation | `GET /api/admin/stock/low`, `GET /api/admin/stock/valuation` |
| Import / export CSV par S3 | `POST /api/admin/catalog/export`, `…/import-url`, `…/import` |
| Recherche et filtres | `GET /api/products?q=&category=&min_price=&max_price=&in_stock=1&sort=price\|-price\|name\|rating` |
| Codes promo | `GET`, `POST /api/admin/promos`, `PUT /api/admin/promos/{code}` ; `promo_code` dans `POST /api/orders` |
| Avis (clients ayant commandé le produit) | `GET`, `POST /api/products/{id}/reviews` |
| Compte client : profil, commandes, liste de souhaits, alertes | `GET /api/me`, `/api/me/orders`, `GET`, `PUT`, `DELETE /api/me/wishlist/{id}`, `GET /api/me/notifications` |
| Paiement, facture | `POST /api/orders/{id}/confirm-payment`, `POST /api/orders/{id}/invoice` (client de la commande ou admin : lien présigné du PDF) |
| Image produit (formulaire présigné, puis rattachement) | `POST /api/admin/products/{id}/image-upload`, `POST /api/admin/products/{id}/image` ; miniatures sous `/images/{id}/{image}-160.webp` et `-480.webp` |
| Retours (client) et réception (magasinier) | `POST /api/orders/{id}/return`, `GET /api/admin/returns`, `POST /api/admin/returns/{id}/receive` |
| Pilotage : tableau de bord, bons de commande, rapport du jour (admin) | `GET /api/admin/dashboard`, `GET /api/admin/purchase-orders`, `POST /api/admin/reports/daily` |
| Suivi en temps réel (AWS) | WebSocket `realtime_url` de `GET /api/health` : `?shop=<solution>&token=<jeton facultatif>`, message `{"action": "subscribe", "order_id": …}` |

**Comptes et rôles** : Amazon Cognito ([infra/auth.tf](infra/auth.tf)), groupes `admin`
(tout), `magasinier` (stock) ; tout utilisateur connecté est client. L'API vérifie elle-même
les jetons ([src/shop/auth.py](src/shop/auth.py), sans dépendance) : signature RS256 avec les
clés publiques du pool, émetteur, client et expiration ; seul le pool configuré est accepté.
En local, un fournisseur d'identité de démonstration ([local/auth_local.py](local/auth_local.py))
remplace Cognito ; il n'existe que dans la stack Docker (`ALLOW_LOCAL_AUTH=1` n'y est défini
que là) et ne peut pas être déployé sur AWS.

Chaque écriture publie un événement (`ORDER_CREATED`, `ORDER_CANCELLED`, `STOCK_CHANGED`,
`PRODUCT_CHANGED`, `PROMO_CHANGED`, `REVIEW_ADDED`, `WISHLIST_CHANGED`, `BACK_IN_STOCK`,
`STOCK_LOW`) : audit S3, e-mails, et projection CQRS de l'hybride. Aucune route `GET` n'écrit.

**Images produit** ([infra/media.tf](infra/media.tf)) : le back-office dépose l'original
directement dans S3 par un formulaire `POST` présigné (clé, type et taille de 5 Mo imposés
par la politique signée), puis la Lambda `thumbnailer` (déclenchée par S3) le vérifie avec
Pillow et écrit deux miniatures WebP, servies par CloudFront sous `/images/*` (cache d'un
an : le nom change à chaque image). Pillow est installé depuis la wheel Linux arm64
(`manylinux2014_aarch64`) à l'empreinte figée ([scripts/lambda-requirements.txt](scripts/lambda-requirements.txt)) par
[scripts/build_lambda_deps.sh](scripts/build_lambda_deps.sh), qui refuse tout binaire qui
n'est pas un ELF aarch64 ; `up.sh` et `down.sh` l'appellent avant Terraform.

**Suivi en temps réel** ([infra/realtime.tf](infra/realtime.tf), AWS seulement) : une API
Gateway WebSocket commune aux trois solutions (facturée au message, sans coût fixe). La
Lambda `realtime` enregistre connexions et abonnements dans une table DynamoDB (TTL de
2 h) ; la Lambda `notifier`, abonnée au topic SNS par une file SQS, pousse les statuts de
commande et les alertes de retour en stock. Le front garde le polling en secours (10 s
au lieu de 2 s quand le WebSocket est ouvert) ; en local, où LocalStack gratuit n'a pas de
WebSocket, il suit les commandes par polling seul.

## Les trois solutions

Les trois solutions sont déployées en parallèle par défaut (`deploy_serverless`, `deploy_vm`,
`deploy_hybrid` dans `terraform.tfvars`) et servent **le même front**
([src/frontend](src/frontend)) qui appelle `/api/*`. Chacune a sa distribution CloudFront.

| | Solution 1 : serverless | Solution 2 : VM | Solution 3 : hybride |
|---|---|---|---|
| Front | S3 (OAC) | servi par les EC2 | S3 (OAC) |
| Catalogue et suivi | Lambda `api` → DynamoDB | EC2 → RDS PostgreSQL | Lambda `hybrid-read` → DynamoDB |
| Commandes | Lambda `api` → transaction DynamoDB | EC2 → transaction SQL (RDS) | EC2 → transaction SQL (RDS) |
| Traitement interne | Step Functions (Lambdas `processor`, `fulfillment`) | worker SQS sur les EC2 | Step Functions (Lambdas `hybrid-processor`, `hybrid-fulfillment`) + `hybrid-projector` |
| Réappro fournisseur | Step Functions (Lambda `supplier`) | worker SQS sur les EC2 | worker SQS `hybrid-restock` sur les EC2 (stock dans RDS) |
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
                                ├─ /api/* écritures  ─► ALB ─► EC2 :8081 ─► RDS (base shop_hybrid)
                                └─ /api/* lectures   ─► Lambda "hybrid-read" ─► DynamoDB (modèle de lecture)
                                                       │
                                     publish           ▼
                              ┌──────────── SNS "order-events" ────────────┐
                              │ filtre source/event_type (pub/sub)          │
     SQS mailer         ◄─────┤ ─► Lambda mailer ─► SES (e-mails, facture)  │
     SQS processing     ◄─────┤ ─► Lambda processor ─► Step Functions       │ (livraison, réappro)
     SQS vm-processing  ◄─────┤ ─► worker sur EC2 (chaque étape)  (VM)      │
     SQS hybrid-restock ◄─────┤ ─► worker sur EC2 hybride (réappro, RDS)    │
     SQS hybrid-projection ◄──┤ ─► Lambda hybrid-projector ─► DynamoDB      │
     SQS hybrid-processing ◄──┤ ─► Lambda hybrid-processor ─► Step Functions│
     SQS audit          ◄─────┤ ─► Lambda audit ─► S3 (archive, Athena)     │
     SQS kafka-bridge   ◄─────┘ ─► EC2 ─► MSK Kafka ─► stats temps réel     │ (option)
```

Cycle de vie d'une commande (identique dans les trois solutions) :

1. `POST /api/orders` : contrôle et décrément du stock **atomique** (transaction
   DynamoDB / `SELECT … FOR UPDATE` PostgreSQL), frais de port, statut `PENDING_PAYMENT`,
   création du PaymentIntent Stripe (clé d'idempotence `threadly-pi-<commande>`),
   événement `ORDER_CREATED`.
2. Le navigateur paie avec Stripe.js ; `POST /api/orders/{id}/confirm-payment` relit le
   PaymentIntent chez Stripe (statut, montant, devise, commande) → `CONFIRMED`,
   `ORDER_PAID` → e-mail de confirmation avec la facture PDF. Une commande restée
   impayée est relue chez Stripe par la purge : confirmée si elle a été payée, sinon
   PaymentIntent puis commande annulés, stock rendu.
3. `ORDER_PAID` lance le traitement : emballage (`PACKED`), expédition (`SHIPPED`,
   transporteur et n° de suivi, e-mail), transit (`IN_TRANSIT`), livraison (`DELIVERED`,
   e-mail). Serverless et hybride : une machine Step Functions par solution
   ([fulfillment.asl.json](infra/templates/fulfillment.asl.json)) ; VM : le worker SQS des
   EC2, une étape par message.
4. Retour : dans les 14 jours suivant la livraison, le client connecté demande
   le retour (`POST /api/orders/{id}/return` → `RETURN_REQUESTED`, étiquette retour
   simulée, e-mail). Le magasinier réceptionne le colis (back-office, `POST
   /api/admin/returns/{id}/receive`) : `RETURNED`, mouvements `RETURN` (stock remis),
   puis remboursement Stripe du total, frais de livraison compris (clé d'idempotence
   `threadly-refund-<commande>` : une réception rejouée ne rembourse jamais deux fois).
   En hybride, ces écritures passent par l'EC2 (statut dans DynamoDB, stock dans RDS).
5. Chaque événement est archivé dans le bucket d'audit (`events/event_type=…/date=…/`).

**Réapprovisionnement fournisseur** : quand un produit passe sous son seuil
(`STOCK_LOW`), un bon de commande est créé (quantité : 4 × le seuil, 10 au moins ; un
seul bon ouvert par produit, garanti par une écriture conditionnelle DynamoDB ou un
index unique partiel PostgreSQL), envoyé par e-mail, puis livré à Paris après le délai
simulé (`fulfillment_delay_seconds`) : mouvement `RESTOCK` de référence le bon.
Serverless : machine Step Functions [restock.asl.json](infra/templates/restock.asl.json)
(Lambda `supplier`) ; VM et hybride : worker SQS sur les EC2, le délai de la file
simulant celui du fournisseur. Le back-office liste les retours à réceptionner et les
bons de commande.

### Pilotage : tableau de bord, rapport quotidien, Athena, X-Ray, Grafana, k6, CI

- **Tableau de bord** du back-office (`GET /api/admin/dashboard`, admin et magasinier) :
  chiffre d'affaires du jour et des 7 derniers jours (commandes payées non remboursées),
  top ventes, ruptures et stocks bas, commandes par statut. En hybride, il est servi par
  la Lambda de lecture (modèle de lecture DynamoDB).
- **Rapport quotidien** : chaque matin à 6 h (Paris), EventBridge Scheduler déclenche la
  Lambda `reporter` (serverless, hybride) ou dépose un message dans la file du worker EC2
  (VM, qui garde tout son traitement sur EC2). CSV des ventes des dernières 24 h, du stock
  par entrepôt et de la valorisation dans `s3://<data>/reports/<solution>/` (30 jours),
  envoyé par SES avec le CSV joint. À la demande : bouton « Envoyer le rapport du jour »
  du back-office (admin), ou `./scripts/daily_report.sh serverless|vm|hybrid [local|aws]`.
- **Athena + Glue** sur le bucket d'audit : base `threadly_dev_audit`, table `events`
  partitionnée par `event_type` et `date` grâce à la projection de partitions (ni crawler
  ni `MSCK REPAIR`), groupe de travail `threadly-dev` (résultats supprimés au bout d'un
  jour, requête annulée au-delà de 100 Mo lus) et 3 requêtes nommées : chiffre d'affaires
  par jour, délai moyen commande → livraison, annulations.
- **X-Ray** : traçage actif sur toutes les Lambdas et les machines Step Functions
  (console X-Ray ou CloudWatch, « Trace Map »).
- **Grafana** (stack locale, http://localhost:3000) : tableau de bord ventes et stock
  provisionné ([local/grafana/](local/grafana/)), sources PostgreSQL des bases VM et
  hybride lues par un compte en lecture seule (le serverless, sur DynamoDB, n'y figure pas).
- **Test de charge k6** : `./scripts/load_test.sh` (local) ou `./scripts/load_test.sh aws`,
  script réutilisable [scripts/load/k6_shop.js](scripts/load/k6_shop.js) lancé par l'image
  Docker `grafana/k6` ; 8 utilisateurs virtuels au plus (quota de 10 Lambdas simultanées),
  parcours de lecture sans commande ni paiement, solutions testées l'une après l'autre,
  résumé comparatif dans `docs/captures/k6_resume.md`.
- **CI** ([.github/workflows/ci.yml](.github/workflows/ci.yml)) : `terraform fmt -check` et
  `terraform validate`, puis la stack Docker et les scénarios, un job par solution en
  parallèle (runners ARM, comme les Lambdas). Aucun secret ni accès AWS.

En local, LocalStack (édition communautaire) n'a ni EventBridge Scheduler, ni Athena,
Glue ou X-Ray : le rapport s'y déclenche à la demande, les scénarios 34 et 35 sont
sans objet.

Pas de webhook Stripe dans ce projet (un secret et un point d'entrée public de plus) :
la purge le remplace. En production, le webhook `payment_intent.succeeded` serait
l'amélioration à faire (voir le rapport).

Le front suit l'avancement en temps réel par WebSocket (API Gateway), avec un polling
de secours toutes les 10 s, ou toutes les 2 s sans WebSocket (en local).
Les mises à jour sont conditionnelles, donc une transition n'est jamais appliquée deux fois.
Si un message est rejoué alors que le statut est déjà à jour (par exemple après un échec de
publication SNS), l'événement suivant est republié pour que la commande ne reste pas bloquée.
Après 3 échecs un message part en Dead Letter Queue, ce qui déclenche une alarme.

### Solution 3 : hybride, routage et cohérence des données

Chaque partie est placée sur le service qui lui convient. Une CloudFront Function choisit
l'origine selon la méthode HTTP ([infra/hybrid.tf](infra/hybrid.tf),
[hybrid_router.js](infra/templates/hybrid_router.js)) :

| Requête | Destination | Pourquoi |
|---|---|---|
| `/*` | S3 (même bucket et OAC que la solution serverless) | Fichiers statiques : rien à calculer |
| `/api/*` en `GET`, `HEAD` | Lambda `hybrid-read` → DynamoDB (lecture seule, IAM compris) | Lectures nombreuses et simples, sans serveur |
| `/api/*` autres méthodes | ALB → EC2 (service `shop-hybrid`, port 8081) → RDS (base `shop_hybrid`) | Toutes les écritures : transactions SQL, sans limite de concurrence Lambda |

**RDS fait foi pour le stock, les produits et la création des commandes ; DynamoDB fait foi
pour le statut des commandes** (CQRS, cohérence à terme). Chaque écriture publie un
événement qui porte les niveaux de stock modifiés et leur version : la Lambda
`hybrid-projector` les recopie dans DynamoDB (une version plus ancienne est ignorée), ainsi
que les produits, les mouvements et les commandes, puis `hybrid-processor` (code du
processor serverless) fait avancer le statut. Le VPC, l'ALB, les EC2, l'instance RDS, le bucket web, SNS et l'audit
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
| API Gateway REST | remplacée par une Lambda Function URL (gratuite) ; API Gateway sert seulement au WebSocket du suivi en temps réel, facturé au message | — |
| Route 53 | pas de domaine, URLs `*.cloudfront.net` | `route53_zone_name = "mondomaine.fr"` |
| DynamoDB | facturation à la requête (`PAY_PER_REQUEST`) : aucun coût au repos, quelques centimes pour les volumes de test ; 25 Go de stockage gratuits | — |
| Logs | rétention 3 jours | `log_retention_days` |

Ce qui peut quand même coûter quelques centimes, même avec le `down` chaque soir :
- l'**ALB** et les **IPv4 publiques** (gratuits seulement pendant les 12 mois du Free Tier « historique ») ;
- **CloudTrail** si le compte a déjà un autre trail (`enable_cloudtrail = false` sinon).

Le budget AWS (créé si `notification_email` est renseigné) envoie un mail dès que
la prévision dépasse 80 % de `monthly_budget_usd`. Dans **Cost Explorer**, on peut
filtrer par le tag `Project = threadly`, posé sur toutes les ressources.

## Sécurité

- **Comptes : Amazon Cognito** ([infra/auth.tf](infra/auth.tf)), groupes `admin` et
  `magasinier` ; chaque route vérifie le rôle (sans jeton 401, mauvais rôle 403).
- **Vérification des jetons** dans l'API elle-même ([src/shop/auth.py](src/shop/auth.py)) :
  RS256 uniquement, signature contrôlée avec la clé publique du pool (JWKS), émetteur,
  client, type de jeton et expiration ; un émetteur autre que Cognito est toujours refusé
  sur AWS (le fournisseur local n'est accepté qu'avec `ALLOW_LOCAL_AUTH=1`, défini dans la
  seule stack Docker).
- **Paiement Stripe et PCI DSS** : la carte est saisie dans Stripe.js (formulaire hébergé
  par Stripe) et ne passe jamais par nos serveurs, ce qui place la boutique dans le
  périmètre PCI DSS le plus réduit (SAQ A). Le serveur relit chaque paiement chez Stripe
  (statut, montant, devise, commande) avant de confirmer ; clés d'idempotence pour les
  paiements et les remboursements ; le `client_secret` n'est renvoyé qu'à la création.
- **Clé secrète Stripe dans SSM Parameter Store** (SecureString), lue à l'exécution, jamais
  dans le code, une variable d'environnement ou le dépôt ; seules les clés de test
  (`sk_test_`, `pk_test_`) sont acceptées. `terraform.tfvars` et `terraform.tfstate` sont
  ignorés par git.
- **IAM** : un rôle par Lambda, et un pour les EC2, chacun avec le strict minimum (moindre privilège).
- Les origines n'acceptent que CloudFront. L'ALB est limité à la *prefix list* CloudFront et vérifie l'en-tête secret `X-Origin-Verify`, que la Lambda vérifie aussi.
- Les buckets S3 sont privés : le site et les miniatures sont servis via Origin Access Control ; les originaux des images ne sont jamais publics.
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
up.sh / down.sh            une commande pour tout créer / tout supprimer
.github/workflows/ci.yml   CI : terraform fmt -check et validate, scénarios sur la stack Docker (3 jobs)
scripts/daily_report.sh    rapport quotidien à la demande (même cible que EventBridge Scheduler)
scripts/load_test.sh       test de charge k6 des trois solutions (scripts/load/k6_shop.js), résumé comparatif
local/grafana/             Grafana local : sources PostgreSQL, tableau de bord provisionné
scripts/test_scenario.sh   scénarios de test (local ou AWS)
scripts/cleanup_orphans.sh ressources "threadly-dev" absentes de l'état Terraform (simulation par défaut ; garde les identités SES)
scripts/check_ses.sh       identité SES vérifiée ? (appelé par up.sh)
scripts/create_demo_admin.sh compte admin de démonstration si demo_admin_email est renseignée (appelé par up.sh)
docs/                      rapport.md et rapport.pdf (corps de 4 pages + annexes A à H), schémas (architecture/*.mmd → PNG), captures/ (session finale)
data/products.json         la "fausse base" : 12 produits (4 t-shirts, 4 pulls, 4 baskets)
src/frontend/              site (HTML/CSS/JS sans dépendance), commun aux 3 solutions
src/lambda/<fonction>/handler.py  code des Lambdas : api, processor, fulfillment, supplier, sweeper, audit,
                           projector, thumbnailer, realtime, notifier, mailer, reporter
src/vm/app.py              appli EC2 (VM et hybride) : site + API + worker SQS (+ Kafka)
infra/                     Terraform
  network.tf               VPC 2 AZ, sous-réseaux publics/privés, endpoints S3/DynamoDB
  database.tf              DynamoDB + seed, RDS PostgreSQL
  storage.tf               buckets web, audit et data (CSV)
  media.tf                 bucket images, Lambda thumbnailer, origine CloudFront /images/*
  realtime.tf              API Gateway WebSocket, table des connexions
  payments.tf              clé Stripe (SSM), contrôle de l'identité SES, machines Step Functions (X-Ray)
  reporting.tf             rapport quotidien (EventBridge Scheduler), Athena + Glue sur le bucket d'audit
  auth.tf                  Cognito (comptes, groupes admin et magasinier)
  messaging.tf             SNS, SQS + DLQ, abonnements filtrés
  lambda.tf                Lambdas, IAM, Function URL, déclencheurs SQS
  vm.tf                    ALB, Auto Scaling Group, launch template, IAM
  cdn.tf / dns.tf          CloudFront, Route 53 + ACM (optionnel)
  hybrid.tf                CloudFront de la solution hybride (routage par méthode HTTP)
  msk.tf                   Kafka (optionnel)
  security.tf              KMS, CloudTrail
  monitoring.tf            alarmes, dashboard, budget
```
