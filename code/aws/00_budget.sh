#!/usr/bin/env bash
# Cost guardrails. No budget is created here (make a monthly AWS Budget with an
# email alert in the console first, e.g. $15); this script only adds
# a Cost Anomaly Detection monitor (DIMENSIONAL/SERVICE) + a daily email
# subscription at a $5 absolute-impact threshold. Skips anything that exists.
set -euo pipefail
source "$(dirname "$0")/config.sh"
CE="aws --region $CE_REGION --no-cli-pager"

log "Existing budgets (not modified)"
$CE budgets describe-budgets --account-id "$ACCOUNT_ID" \
  --query 'Budgets[].[BudgetName,BudgetLimit.Amount,BudgetLimit.Unit,TimeUnit]' --output table

log "Cost anomaly monitor"
# AWS allows only one DIMENSIONAL/SERVICE monitor per account, so reuse any existing one.
MON_ARN=$($CE ce get-anomaly-monitors \
  --query "AnomalyMonitors[?MonitorType=='DIMENSIONAL' && MonitorDimension=='SERVICE'].MonitorArn | [0]" --output text)
if [[ $MON_ARN == None || -z $MON_ARN ]]; then
  MON_ARN=$($CE ce create-anomaly-monitor \
    --anomaly-monitor "MonitorName=${PREFIX}-services,MonitorType=DIMENSIONAL,MonitorDimension=SERVICE" \
    --resource-tags "Key=$TAG_KEY,Value=$TAG_VAL" \
    --query MonitorArn --output text)
  echo "created monitor $MON_ARN"
else
  echo "reusing monitor $MON_ARN"
fi

log "Cost anomaly subscription (DAILY email to $BUDGET_EMAIL, impact >= \$5)"
SUB_NAME="${PREFIX}-daily"
EXISTS=$($CE ce get-anomaly-subscriptions \
  --query "AnomalySubscriptions[?SubscriptionName=='$SUB_NAME'].SubscriptionArn | [0]" --output text)
if [[ $EXISTS == None || -z $EXISTS ]]; then
  $CE ce create-anomaly-subscription \
    --resource-tags "Key=$TAG_KEY,Value=$TAG_VAL" \
    --anomaly-subscription "$(cat <<JSON
{
  "SubscriptionName": "$SUB_NAME",
  "MonitorArnList": ["$MON_ARN"],
  "Subscribers": [{"Type": "EMAIL", "Address": "$BUDGET_EMAIL"}],
  "Frequency": "DAILY",
  "ThresholdExpression": {"Dimensions": {"Key": "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
    "MatchOptions": ["GREATER_THAN_OR_EQUAL"], "Values": ["5"]}}
}
JSON
)"
  echo "created subscription $SUB_NAME"
else
  echo "subscription exists: $EXISTS"
fi
echo "Done. (Anomaly detection needs ~10 days of history before it alerts reliably.)"
