#!/usr/bin/env bash
# Supprime les ressources AWS "threadly-dev" qu'aucun état Terraform ne connaît
# (par exemple après deux `terraform apply` lancés en même temps).
#
#   ./scripts/cleanup_orphans.sh           # simulation : affiche ce qui serait supprimé
#   ./scripts/cleanup_orphans.sh --apply   # supprime réellement
#
# Toute ressource présente dans infra/terraform.tfstate est ignorée : elle est
# supprimée par ./down.sh. Variables : PREFIX (threadly-dev), AWS_REGION (eu-west-3).
# RDS est supprimée sans snapshot final ; les buckets sont vidés, versions comprises.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${PREFIX:-threadly-dev}"
STATE="$ROOT/infra/terraform.tfstate"
export AWS_REGION="${AWS_REGION:-eu-west-3}" AWS_PAGER=""

APPLY=false
[ "${1:-}" = "--apply" ] && APPLY=true

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

DELETED=0
KEPT=0
ERRORS=0

# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

section() { printf '\n\033[1m━━ %s\033[0m\n' "$*"; }
info() { printf '      %s\n' "$*"; }

# names COMMANDE_AWS... : une valeur par ligne, sans les "None" de --output text.
names() {
  aws "$@" --output text 2>"$TMP/list.err" | tr '\t' '\n' | grep -v -e '^None$' -e '^$'
  if [ -s "$TMP/list.err" ]; then
    printf '   ⚠️  lecture impossible (%s) : %s\n' "$1 $2" "$(tail -1 "$TMP/list.err")" >&2
  fi
}

# Index de l'état : une ligne "type<TAB>valeur" par attribut texte de chaque ressource
# gérée. Le type compte : "threadly-dev-db" est à la fois une instance RDS et un
# groupe de sécurité, qui peuvent appartenir à des déploiements différents.
STATE_INDEX="$TMP/state.idx"
: >"$STATE_INDEX"
if [ -f "$STATE" ]; then
  python3 - "$STATE" >"$STATE_INDEX" <<'EOF'
import json, sys
for r in json.load(open(sys.argv[1])).get("resources", []):
    if r.get("mode") != "managed":
        continue
    for i in r.get("instances", []):
        for v in (i.get("attributes") or {}).values():
            if isinstance(v, str) and v:
                print(f"{r['type']}\t{v}")
EOF
fi

# orphan TYPE_TERRAFORM NOM_OU_ID : vrai si aucune ressource de ce type ne l'a dans l'état.
orphan() {
  if grep -qxF "$1	$2" "$STATE_INDEX"; then
    printf '   ⏭️  %s : dans terraform.tfstate (%s), laissé à down.sh\n' "$2" "$1"
    KEPT=$((KEPT + 1))
    return 1
  fi
}

# act DESCRIPTION COMMANDE... : simule ou exécute une suppression.
act() {
  local what="$1"
  shift
  if ! $APPLY; then
    printf '   🗑️  [simulation] %s\n' "$what"
    DELETED=$((DELETED + 1))
    return 0
  fi
  printf '   … %s\n' "$what"
  if "$@" >"$TMP/out" 2>"$TMP/err"; then
    printf '   ✅ %s\n' "$what"
    DELETED=$((DELETED + 1))
  else
    printf '   ❌ %s : %s\n' "$what" "$(tail -1 "$TMP/err")"
    ERRORS=$((ERRORS + 1))
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Suppressions en plusieurs étapes
# ---------------------------------------------------------------------------

delete_lambda() {
  for uuid in $(names lambda list-event-source-mappings --function-name "$1" --query 'EventSourceMappings[].UUID'); do
    aws lambda delete-event-source-mapping --uuid "$uuid" || return 1
  done
  aws lambda delete-function --function-name "$1"
}

# CloudFront exige l'ETag courant, et refuse (…InUse) si une distribution s'en sert encore.
delete_cache_policy() {
  local etag
  etag=$(aws cloudfront get-cache-policy --id "$1" --query ETag --output text) || return 1
  aws cloudfront delete-cache-policy --id "$1" --if-match "$etag"
}

# Une distribution doit d'abord être désactivée, puis redéployée (15 min environ),
# avant d'accepter la suppression avec son nouvel ETag.
delete_distribution() {
  local etag
  aws cloudfront get-distribution-config --id "$1" >"$TMP/dist.json" || return 1
  etag=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ETag"])' "$TMP/dist.json")
  if [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["DistributionConfig"]["Enabled"])' "$TMP/dist.json")" = True ]; then
    python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["DistributionConfig"]; c["Enabled"]=False; json.dump(c,open(sys.argv[2],"w"))' \
      "$TMP/dist.json" "$TMP/dist-off.json"
    aws cloudfront update-distribution --id "$1" --if-match "$etag" \
      --distribution-config "file://$TMP/dist-off.json" >/dev/null || return 1
  fi
  aws cloudfront wait distribution-deployed --id "$1" || return 1
  etag=$(aws cloudfront get-distribution --id "$1" --query ETag --output text) || return 1
  aws cloudfront delete-distribution --id "$1" --if-match "$etag"
}

delete_function() {
  local etag
  etag=$(aws cloudfront describe-function --name "$1" --query ETag --output text) || return 1
  aws cloudfront delete-function --name "$1" --if-match "$etag"
}

delete_oac() {
  local etag
  etag=$(aws cloudfront get-origin-access-control --id "$1" --query ETag --output text) || return 1
  aws cloudfront delete-origin-access-control --id "$1" --if-match "$etag"
}

delete_rds() {
  aws rds delete-db-instance --db-instance-identifier "$1" \
    --skip-final-snapshot --delete-automated-backups || return 1
  aws rds wait db-instance-deleted --db-instance-identifier "$1"
}

delete_alb() {
  aws elbv2 delete-load-balancer --load-balancer-arn "$1" || return 1
  aws elbv2 wait load-balancers-deleted --load-balancer-arns "$1"
}

delete_table() {
  aws dynamodb delete-table --table-name "$1" || return 1
  aws dynamodb wait table-not-exists --table-name "$1"
}

delete_role() {
  local role="$1"
  for p in $(names iam list-role-policies --role-name "$role" --query 'PolicyNames'); do
    aws iam delete-role-policy --role-name "$role" --policy-name "$p" || return 1
  done
  for arn in $(names iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn'); do
    aws iam detach-role-policy --role-name "$role" --policy-arn "$arn" || return 1
  done
  for prof in $(names iam list-instance-profiles-for-role --role-name "$role" --query 'InstanceProfiles[].InstanceProfileName'); do
    aws iam remove-role-from-instance-profile --instance-profile-name "$prof" --role-name "$role" || return 1
    aws iam delete-instance-profile --instance-profile-name "$prof" || return 1
  done
  aws iam delete-role --role-name "$role"
}

# Vide le bucket par lots de 1000 : versions d'objets et marqueurs de suppression
# (un bucket non versionné renvoie ses objets avec VersionId "null").
delete_bucket() {
  local bucket="$1" n
  while :; do
    aws s3api list-object-versions --bucket "$bucket" --max-items 1000 --output json >"$TMP/versions.json" || return 1
    n=$(python3 - "$TMP/versions.json" "$TMP/delete.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
objects = [{"Key": o["Key"], "VersionId": o["VersionId"]}
           for o in (d.get("Versions") or []) + (d.get("DeleteMarkers") or [])]
json.dump({"Objects": objects, "Quiet": True}, open(sys.argv[2], "w"))
print(len(objects))
EOF
)
    [ "$n" = 0 ] && break
    aws s3api delete-objects --bucket "$bucket" --delete "file://$TMP/delete.json" >/dev/null || return 1
  done
  aws s3api delete-bucket --bucket "$bucket"
}

describe_bucket() {
  local versioning versions markers
  versioning=$(aws s3api get-bucket-versioning --bucket "$1" --query 'Status' --output text 2>/dev/null)
  versions=$(aws s3api list-object-versions --bucket "$1" --query 'length(Versions || `[]`)' --output text 2>/dev/null)
  markers=$(aws s3api list-object-versions --bucket "$1" --query 'length(DeleteMarkers || `[]`)' --output text 2>/dev/null)
  [ "$versioning" = None ] && versioning="jamais activé"
  info "versioning : $versioning, $versions objet(s)/version(s), $markers marqueur(s) de suppression"
}

# Attend qu'il ne reste plus d'interface réseau dans la VPC (ALB, RDS, Lambda...).
wait_enis() {
  local i
  for i in $(seq 1 60); do
    [ -z "$(names ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$1" --query 'NetworkInterfaces[].NetworkInterfaceId')" ] && return 0
    sleep 15
  done
  echo "interfaces réseau encore présentes après 15 min" >&2
  return 1
}

delete_vpc() {
  local vpc="$1" ids sg rules i
  ids=$(names ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$vpc" --query 'VpcEndpoints[].VpcEndpointId')
  if [ -n "$ids" ]; then
    aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $ids >/dev/null || return 1
    for i in $(seq 1 40); do
      [ -z "$(names ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$vpc" "Name=vpc-endpoint-state,Values=pending,available,deleting" --query 'VpcEndpoints[].VpcEndpointId')" ] && break
      sleep 5
    done
  fi
  wait_enis "$vpc" || return 1
  # Les groupes de sécurité se référencent entre eux : on retire d'abord toutes les règles.
  for sg in $(names ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" --query 'SecurityGroups[?GroupName!=`default`].GroupId'); do
    rules=$(names ec2 describe-security-group-rules --filters "Name=group-id,Values=$sg" --query 'SecurityGroupRules[?!IsEgress].SecurityGroupRuleId')
    [ -n "$rules" ] && { aws ec2 revoke-security-group-ingress --group-id "$sg" --security-group-rule-ids $rules >/dev/null || return 1; }
    rules=$(names ec2 describe-security-group-rules --filters "Name=group-id,Values=$sg" --query 'SecurityGroupRules[?IsEgress].SecurityGroupRuleId')
    [ -n "$rules" ] && { aws ec2 revoke-security-group-egress --group-id "$sg" --security-group-rule-ids $rules >/dev/null || return 1; }
  done
  for sg in $(names ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" --query 'SecurityGroups[?GroupName!=`default`].GroupId'); do
    aws ec2 delete-security-group --group-id "$sg" || return 1
  done
  for subnet in $(names ec2 describe-subnets --filters "Name=vpc-id,Values=$vpc" --query 'Subnets[].SubnetId'); do
    aws ec2 delete-subnet --subnet-id "$subnet" || return 1
  done
  for rt in $(names ec2 describe-route-tables --filters "Name=vpc-id,Values=$vpc" --query 'RouteTables[?!(Associations[?Main])].RouteTableId'); do
    aws ec2 delete-route-table --route-table-id "$rt" || return 1
  done
  for igw in $(names ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$vpc" --query 'InternetGateways[].InternetGatewayId'); do
    aws ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc" || return 1
    aws ec2 delete-internet-gateway --internet-gateway-id "$igw" || return 1
  done
  aws ec2 delete-vpc --vpc-id "$vpc"
}

describe_vpc() {
  local vpc="$1"
  info "sous-réseaux : $(names ec2 describe-subnets --filters "Name=vpc-id,Values=$vpc" --query 'Subnets[].SubnetId' | tr '\n' ' ')"
  info "groupes de sécurité : $(names ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc" --query 'SecurityGroups[?GroupName!=`default`].GroupName' | tr '\n' ' ')"
  info "endpoints : $(names ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$vpc" --query 'VpcEndpoints[].ServiceName' | tr '\n' ' ')"
  info "passerelle Internet : $(names ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$vpc" --query 'InternetGateways[].InternetGatewayId' | tr '\n' ' ')"
  info "tables de routage (hors principale) : $(names ec2 describe-route-tables --filters "Name=vpc-id,Values=$vpc" --query 'RouteTables[?!(Associations[?Main])].RouteTableId' | tr '\n' ' ')"
  info "interfaces réseau : $(names ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$vpc" --query 'NetworkInterfaces[].Description' | tr '\n' ';')"
}

# ---------------------------------------------------------------------------

ACCOUNT=$(aws sts get-caller-identity --query Account --output text) || exit 1
section "$($APPLY && echo SUPPRESSION || echo SIMULATION) des ressources orphelines \"$PREFIX\""
info "Compte $ACCOUNT, région $AWS_REGION"
if [ -f "$STATE" ]; then
  info "État Terraform : $STATE ($(grep -c '"mode": "managed"' "$STATE") blocs gérés)"
else
  info "État Terraform : aucun (toutes les ressources $PREFIX seront traitées)"
fi

section "CloudTrail"
for t in $(names cloudtrail describe-trails --query "trailList[?starts_with(Name,'$PREFIX')].Name"); do
  orphan aws_cloudtrail "$t" && act "trail $t" aws cloudtrail delete-trail --name "$t"
done

section "CloudFront"
for d in $(names cloudfront list-distributions --query "DistributionList.Items[?contains(Comment,'$PREFIX')].Id"); do
  if orphan aws_cloudfront_distribution "$d"; then
    info "$(aws cloudfront get-distribution --id "$d" --query 'Distribution.[DomainName,Status,DistributionConfig.Enabled,DistributionConfig.Comment]' --output text)"
    act "distribution $d (désactivation, attente du déploiement ~15 min, suppression)" delete_distribution "$d"
  fi
done
for f in $(names cloudfront list-functions --query "FunctionList.Items[?starts_with(Name,'$PREFIX')].Name" | sort -u); do
  orphan aws_cloudfront_function "$f" && act "CloudFront Function $f" delete_function "$f"
done
for p in $(names cloudfront list-cache-policies --type custom --query "CachePolicyList.Items[?starts_with(CachePolicy.CachePolicyConfig.Name,'$PREFIX')].CachePolicy.Id"); do
  orphan aws_cloudfront_cache_policy "$p" && act "cache policy $p" delete_cache_policy "$p"
done
for o in $(names cloudfront list-origin-access-controls --query "OriginAccessControlList.Items[?starts_with(Name,'$PREFIX')].Id"); do
  orphan aws_cloudfront_origin_access_control "$o" && act "Origin Access Control $o" delete_oac "$o"
done

section "Auto Scaling, EC2, Lambda, Step Functions, API Gateway"
for g in $(names autoscaling describe-auto-scaling-groups --query "AutoScalingGroups[?starts_with(AutoScalingGroupName,'$PREFIX')].AutoScalingGroupName"); do
  orphan aws_autoscaling_group "$g" && act "Auto Scaling Group $g (et ses instances)" aws autoscaling delete-auto-scaling-group --auto-scaling-group-name "$g" --force-delete
done
for lt in $(names ec2 describe-launch-templates --query "LaunchTemplates[?starts_with(LaunchTemplateName,'$PREFIX')].LaunchTemplateId"); do
  orphan aws_launch_template "$lt" && act "launch template $lt" aws ec2 delete-launch-template --launch-template-id "$lt"
done
for f in $(names lambda list-functions --query "Functions[?starts_with(FunctionName,'$PREFIX')].FunctionName"); do
  orphan aws_lambda_function "$f" && act "Lambda $f" delete_lambda "$f"
done
for sm in $(names stepfunctions list-state-machines --query "stateMachines[?starts_with(name,'$PREFIX')].stateMachineArn"); do
  orphan aws_sfn_state_machine "$sm" && act "machine d'état ${sm##*:}" aws stepfunctions delete-state-machine --state-machine-arn "$sm"
done
for api in $(names apigatewayv2 get-apis --query "Items[?starts_with(Name,'$PREFIX')].ApiId"); do
  orphan aws_apigatewayv2_api "$api" && act "API WebSocket $api (routes et étapes comprises)" aws apigatewayv2 delete-api --api-id "$api"
done

section "RDS"
for db in $(names rds describe-db-instances --query "DBInstances[?starts_with(DBInstanceIdentifier,'$PREFIX')].DBInstanceIdentifier"); do
  if orphan aws_db_instance "$db"; then
    info "$(aws rds describe-db-instances --db-instance-identifier "$db" --query 'DBInstances[0].[DBInstanceClass,DBInstanceStatus,DBSubnetGroup.VpcId]' --output text)"
    act "instance RDS $db, sans snapshot final (attente de la suppression, 5-10 min)" delete_rds "$db"
  fi
done
for sg in $(names rds describe-db-subnet-groups --query "DBSubnetGroups[?starts_with(DBSubnetGroupName,'$PREFIX')].DBSubnetGroupName"); do
  orphan aws_db_subnet_group "$sg" && act "groupe de sous-réseaux RDS $sg" aws rds delete-db-subnet-group --db-subnet-group-name "$sg"
done

section "Load balancer"
for arn in $(names elbv2 describe-load-balancers --query "LoadBalancers[?starts_with(LoadBalancerName,'$PREFIX')].LoadBalancerArn"); do
  orphan aws_lb "$arn" && act "ALB ${arn##*:loadbalancer/} (avec ses listeners)" delete_alb "$arn"
done
for arn in $(names elbv2 describe-target-groups --query "TargetGroups[?starts_with(TargetGroupName,'$PREFIX')].TargetGroupArn"); do
  orphan aws_lb_target_group "$arn" && act "target group ${arn##*:targetgroup/}" aws elbv2 delete-target-group --target-group-arn "$arn"
done

section "DynamoDB"
for t in $(names dynamodb list-tables --query "TableNames[?starts_with(@,'$PREFIX')]"); do
  orphan aws_dynamodb_table "$t" && act "table $t" delete_table "$t"
done

section "IAM"
for r in $(names iam list-roles --query "Roles[?starts_with(RoleName,'$PREFIX')].RoleName"); do
  orphan aws_iam_role "$r" && act "rôle $r (politiques et profil d'instance compris)" delete_role "$r"
done
for p in $(names iam list-instance-profiles --query "InstanceProfiles[?starts_with(InstanceProfileName,'$PREFIX') && length(Roles)==\`0\`].InstanceProfileName"); do
  orphan aws_iam_instance_profile "$p" && act "profil d'instance $p" aws iam delete-instance-profile --instance-profile-name "$p"
done

section "SSM Parameter Store"
for p in $(names ssm describe-parameters --parameter-filters "Key=Name,Option=BeginsWith,Values=/$PREFIX/" --query 'Parameters[].Name'); do
  orphan aws_ssm_parameter "$p" && act "paramètre $p" aws ssm delete-parameter --name "$p"
done

section "S3"
for b in $(names s3api list-buckets --query "Buckets[?starts_with(Name,'$PREFIX-')].Name"); do
  if orphan aws_s3_bucket "$b"; then
    describe_bucket "$b"
    act "bucket $b (vidé, versions comprises)" delete_bucket "$b"
  fi
done

section "VPC"
for vpc in $(names ec2 describe-vpcs --filters "Name=tag:Name,Values=$PREFIX-vpc" --query 'Vpcs[].VpcId'); do
  if orphan aws_vpc "$vpc"; then
    describe_vpc "$vpc"
    act "VPC $vpc et son contenu (après disparition des interfaces réseau)" delete_vpc "$vpc"
  fi
done

section "CloudWatch, SNS, SQS, budget"
for a in $(names cloudwatch describe-alarms --alarm-name-prefix "$PREFIX" --query 'MetricAlarms[].AlarmName'); do
  orphan aws_cloudwatch_metric_alarm "$a" && act "alarme $a" aws cloudwatch delete-alarms --alarm-names "$a"
done
for d in $(names cloudwatch list-dashboards --dashboard-name-prefix "$PREFIX" --query 'DashboardEntries[].DashboardName'); do
  orphan aws_cloudwatch_dashboard "$d" && act "tableau de bord $d" aws cloudwatch delete-dashboards --dashboard-names "$d"
done
for prefix in "/aws/lambda/$PREFIX" "/aws/msk/$PREFIX"; do
  for lg in $(names logs describe-log-groups --log-group-name-prefix "$prefix" --query 'logGroups[].logGroupName'); do
    orphan aws_cloudwatch_log_group "$lg" && act "groupe de logs $lg" aws logs delete-log-group --log-group-name "$lg"
  done
done
for t in $(names sns list-topics --query "Topics[?contains(TopicArn,':$PREFIX-')].TopicArn"); do
  orphan aws_sns_topic "$t" && act "topic SNS ${t##*:} (et ses abonnements)" aws sns delete-topic --topic-arn "$t"
done
for q in $(names sqs list-queues --queue-name-prefix "$PREFIX" --query 'QueueUrls'); do
  orphan aws_sqs_queue "$q" && act "file SQS ${q##*/}" aws sqs delete-queue --queue-url "$q"
done
for b in $(names budgets describe-budgets --account-id "$ACCOUNT" --query "Budgets[?starts_with(BudgetName,'$PREFIX')].BudgetName"); do
  orphan aws_budgets_budget "$b" && act "budget $b" aws budgets delete-budget --account-id "$ACCOUNT" --budget-name "$b"
done

# ---------------------------------------------------------------------------
section "Scheduler, Athena, Glue (lot 4)"
for sc in $(names scheduler list-schedules --name-prefix "$PREFIX" --query 'Schedules[].Name'); do
  orphan aws_scheduler_schedule "$sc" && act "planification $sc" aws scheduler delete-schedule --name "$sc"
done
for wg in $(names athena list-work-groups --query "WorkGroups[?starts_with(Name,'$PREFIX')].Name"); do
  orphan aws_athena_workgroup "$wg" && act "groupe de travail Athena $wg (et ses requêtes nommées)" \
    aws athena delete-work-group --work-group "$wg" --recursive-delete-option
done
GLUE_DB="$(echo "${PREFIX}_audit" | tr '-' '_')"
for db in $(names glue get-databases --query "DatabaseList[?Name=='$GLUE_DB'].Name"); do
  orphan aws_glue_catalog_database "$db" && act "base Glue $db (et ses tables)" aws glue delete-database --name "$db"
done

# ---------------------------------------------------------------------------
# Identités SES : vérifiées une fois à la main, hors Terraform (README, « Vérification
# SES »). Jamais supprimées : une nouvelle vérification redemanderait un clic par e-mail.
section "SES (à conserver)"
for id in $(names sesv2 list-email-identities --query 'EmailIdentities[].IdentityName'); do
  printf '   🔒 identité SES %s : vérifiée hors Terraform, NE PAS supprimer\n' "$id"
  KEPT=$((KEPT + 1))
done

# ---------------------------------------------------------------------------
section "Résumé"
if $APPLY; then
  info "Supprimées : $DELETED, laissées en place : $KEPT, erreurs : $ERRORS"
else
  info "À supprimer : $DELETED, laissées en place : $KEPT, à traiter à la main : $ERRORS"
  info "Rien n'a été supprimé. Pour supprimer : $0 --apply"
fi
exit $((ERRORS > 0))
