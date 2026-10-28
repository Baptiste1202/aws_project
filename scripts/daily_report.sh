#!/usr/bin/env bash
# Déclenche à la demande le rapport quotidien d'une solution, exactement comme le fait
# chaque matin EventBridge Scheduler (infra/reporting.tf) : même cible, même message.
#
#   ./scripts/daily_report.sh serverless|vm|hybrid [local|aws]   # aws par défaut
#
#   serverless, hybrid : invocation de la Lambda reporter (hybrid-reporter) ;
#   vm                 : message DAILY_REPORT_REQUESTED dans la file vm-processing,
#                        traité par le worker EC2 (après le délai de la file).
# Le CSV arrive dans s3://<bucket data>/reports/<solution>/ et part par e-mail (SES).
set -euo pipefail

SOLUTION="${1:?solution : serverless, vm ou hybrid}"
MODE="${2:-aws}"
export AWS_PAGER=""
if [ "$MODE" = local ]; then
  export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=eu-west-3
  AWS=(aws --endpoint-url "${LOCALSTACK_URL:-http://localhost:4566}")
  PREFIX=threadly
else
  export AWS_REGION="${AWS_REGION:-eu-west-3}"
  AWS=(aws)
  PREFIX=threadly-dev
fi

case "$SOLUTION" in
  serverless | hybrid)
    fn="$PREFIX-reporter"
    [ "$SOLUTION" = hybrid ] && fn="$PREFIX-hybrid-reporter"
    out="$(mktemp)"
    "${AWS[@]}" lambda invoke --function-name "$fn" --cli-read-timeout 90 "$out" >/dev/null
    cat "$out"
    echo
    rm -f "$out"
    ;;
  vm)
    url="$("${AWS[@]}" sqs get-queue-url --queue-name "$PREFIX-vm-processing" --query QueueUrl --output text)"
    "${AWS[@]}" sqs send-message --queue-url "$url" --message-body '{"event_type": "DAILY_REPORT_REQUESTED"}' \
      --query MessageId --output text
    echo "message déposé dans $PREFIX-vm-processing : le worker EC2 produit le rapport après le délai de la file"
    ;;
  *)
    echo "solution inconnue : $SOLUTION (serverless, vm ou hybrid)" >&2
    exit 2
    ;;
esac
