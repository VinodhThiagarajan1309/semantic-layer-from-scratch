#!/usr/bin/env bash
# Tear down everything this demo created, in dependency order, then list any
# leftovers. Safe to re-run. `--all` also removes the cost-anomaly monitor and
# subscription from 00_budget.sh (the pre-existing budgets are never touched).
# Never deletes $PROTECTED_SUBNET (set it in config.sh if you have one to protect).
set -euo pipefail
source "$(dirname "$0")/config.sh"
ALL=false; [[ ${1:-} == --all ]] && ALL=true
VPC=$(default_vpc)

gone_wait() { # gone_wait LABEL cmd...  (poll until cmd fails, i.e. resource gone)
  local label=$1; shift
  for _ in $(seq 1 90); do "$@" >/dev/null 2>&1 || { echo "  $label deleted"; return 0; }
    echo "  waiting for $label..."; sleep 20; done
  echo "timed out waiting for $label" >&2; return 1
}

log "Redshift usage limits"
WG_ARN=$($AWS redshift-serverless get-workgroup --workgroup-name "$RS_WORKGROUP" \
  --query workgroup.workgroupArn --output text 2>/dev/null || true)
if [[ -n $WG_ARN ]]; then
  for id in $($AWS redshift-serverless list-usage-limits --resource-arn "$WG_ARN" --query 'usageLimits[].usageLimitId' --output text); do
    $AWS redshift-serverless delete-usage-limit --usage-limit-id "$id" >/dev/null && echo "  deleted $id" || echo "  FAILED $id"
  done
fi

log "Redshift workgroup $RS_WORKGROUP"
if [[ -n $WG_ARN ]]; then
  $AWS redshift-serverless delete-workgroup --workgroup-name "$RS_WORKGROUP" >/dev/null
  gone_wait workgroup $AWS redshift-serverless get-workgroup --workgroup-name "$RS_WORKGROUP"
else echo "  not found"; fi

log "Redshift namespace $RS_NAMESPACE (no final snapshot)"
if $AWS redshift-serverless get-namespace --namespace-name "$RS_NAMESPACE" >/dev/null 2>&1; then
  $AWS redshift-serverless delete-namespace --namespace-name "$RS_NAMESPACE" >/dev/null
  gone_wait namespace $AWS redshift-serverless get-namespace --namespace-name "$RS_NAMESPACE"
else echo "  not found"; fi

log "Aurora instance $AURORA_INSTANCE"
if $AWS rds describe-db-instances --db-instance-identifier "$AURORA_INSTANCE" >/dev/null 2>&1; then
  $AWS rds delete-db-instance --db-instance-identifier "$AURORA_INSTANCE" --skip-final-snapshot >/dev/null
  $AWS rds wait db-instance-deleted --db-instance-identifier "$AURORA_INSTANCE" && echo "  deleted"
else echo "  not found"; fi

log "Aurora cluster $AURORA_CLUSTER (skip final snapshot, delete automated backups)"
if $AWS rds describe-db-clusters --db-cluster-identifier "$AURORA_CLUSTER" >/dev/null 2>&1; then
  $AWS rds delete-db-cluster --db-cluster-identifier "$AURORA_CLUSTER" \
    --skip-final-snapshot --delete-automated-backups >/dev/null
  gone_wait cluster $AWS rds describe-db-clusters --db-cluster-identifier "$AURORA_CLUSTER"
else echo "  not found"; fi

log "DB subnet group $DB_SUBNET_GROUP"
$AWS rds delete-db-subnet-group --db-subnet-group-name "$DB_SUBNET_GROUP" 2>/dev/null && echo "  deleted" || echo "  not found"

log "Security group $SG_NAME"
SG=$(sg_id "$VPC")
if [[ $SG != None ]]; then
  # Redshift/RDS ENIs can linger a few minutes after deletion; retry.
  for _ in $(seq 1 30); do
    $AWS ec2 delete-security-group --group-id "$SG" 2>/dev/null && { echo "  deleted $SG"; break; }
    echo "  SG still in use (ENIs detaching), retrying..."; sleep 20
  done
else echo "  not found"; fi

log "Tagged subnets"
for id in $(our_subnets "$VPC"); do
  [[ $id == "$PROTECTED_SUBNET" ]] && { echo "  skipping protected $id"; continue; }
  $AWS ec2 delete-subnet --subnet-id "$id" && echo "  deleted $id" || echo "  FAILED $id (still in use?)"
done

if $ALL; then
  log "--all: cost anomaly subscription + monitor"
  CE="aws --region $CE_REGION --no-cli-pager"
  for arn in $($CE ce get-anomaly-subscriptions --query "AnomalySubscriptions[?SubscriptionName=='${PREFIX}-daily'].SubscriptionArn" --output text); do
    $CE ce delete-anomaly-subscription --subscription-arn "$arn" && echo "  deleted $arn" || echo "  FAILED $arn"; done
  for arn in $($CE ce get-anomaly-monitors --query "AnomalyMonitors[?MonitorName=='${PREFIX}-services'].MonitorArn" --output text); do
    $CE ce delete-anomaly-monitor --monitor-arn "$arn" && echo "  deleted $arn" || echo "  FAILED $arn"; done
else
  echo; echo "(Keeping cost-anomaly monitor; pass --all to remove it. Existing budgets are never touched.)"
fi

log "Leftover check (everything below should be empty)"
echo "RDS clusters/instances/subnet groups:"
$AWS rds describe-db-clusters --query "DBClusters[?starts_with(DBClusterIdentifier,'$PREFIX')].[DBClusterIdentifier,Status]" --output text
$AWS rds describe-db-instances --query "DBInstances[?starts_with(DBInstanceIdentifier,'$PREFIX')].[DBInstanceIdentifier,DBInstanceStatus]" --output text
$AWS rds describe-db-subnet-groups --query "DBSubnetGroups[?starts_with(DBSubnetGroupName,'$PREFIX')].DBSubnetGroupName" --output text
$AWS rds describe-db-cluster-snapshots --query "DBClusterSnapshots[?starts_with(DBClusterIdentifier,'$PREFIX')].DBClusterSnapshotIdentifier" --output text
echo "Redshift Serverless workgroups/namespaces/snapshots:"
$AWS redshift-serverless list-workgroups --query "workgroups[?starts_with(workgroupName,'$PREFIX')].[workgroupName,status]" --output text
$AWS redshift-serverless list-namespaces --query "namespaces[?starts_with(namespaceName,'$PREFIX')].[namespaceName,status]" --output text
$AWS redshift-serverless list-snapshots --query "snapshots[?starts_with(namespaceName,'$PREFIX')].snapshotName" --output text
echo "EC2 SGs/subnets:"
$AWS ec2 describe-security-groups --filters "Name=group-name,Values=${PREFIX}*" --query 'SecurityGroups[].GroupId' --output text
$AWS ec2 describe-subnets --filters "Name=tag:$TAG_KEY,Values=$TAG_VAL" --query 'Subnets[].SubnetId' --output text
echo "Elastic IPs (each costs \$0.005/hr, attached or not):"
$AWS ec2 describe-addresses --query 'Addresses[].[PublicIp,AllocationId,NetworkInterfaceId]' --output text
echo "Anything still tagged $TAG_KEY=$TAG_VAL (tag index can lag a few minutes):"
$AWS resourcegroupstaggingapi get-resources --tag-filters "Key=$TAG_KEY,Values=$TAG_VAL" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text
echo "Teardown complete."
