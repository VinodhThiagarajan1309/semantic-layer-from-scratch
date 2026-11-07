#!/usr/bin/env bash
# Redshift Serverless: namespace + 4-RPU workgroup (base=max=4), public, in our
# 3 subnets / 3 AZs, with a usage limit (1 RPU-hour/day; raise with update-usage-limit if tests run out) that DEACTIVATES the
# workgroup when breached. Writes REDSHIFT_* to .env.aws and lists public IPs.
set -euo pipefail
source "$(dirname "$0")/config.sh"
VPC=$(default_vpc); SG=$(sg_id "$VPC"); SUBNETS=$(our_subnets "$VPC")
[[ $SG != None ]] || { echo "Run 01_network.sh first" >&2; exit 1; }
# Rule relied on: a Redshift Serverless workgroup needs at least 3 subnets in
# 3 different AZs (Redshift Mgmt Guide, "Considerations when using Amazon
# Redshift Serverless"; not stated in CLI help). All subnets must also be in
# AZ IDs Redshift Serverless supports; usw2-az1/az2/az3 are used for that reason.
# shellcheck disable=SC2086
AZS=$($AWS ec2 describe-subnets --subnet-ids $SUBNETS --query 'Subnets[].AvailabilityZone' --output text | tr '\t' '\n' | sort -u | wc -l)
(( AZS >= 3 )) || { echo "Need subnets in >=3 AZs, have $AZS" >&2; exit 1; }

# Poll until a status query prints the wanted value (no CLI waiters for redshift-serverless).
wait_for() { # wait_for WANT cmd...
  local want=$1; shift; local s
  for _ in $(seq 1 90); do
    s=$("$@" 2>/dev/null || echo MISSING); echo "  status: $s"
    [[ $s == "$want" ]] && return 0; sleep 20
  done
  echo "timed out waiting for $want" >&2; return 1
}

log "Namespace $RS_NAMESPACE"
if $AWS redshift-serverless get-namespace --namespace-name "$RS_NAMESPACE" >/dev/null 2>&1; then
  echo "exists; keeping the password already in $ENV_FILE"
  PW=$(get_env REDSHIFT_PASSWORD)
else
  PW=$(gen_password 24)            # 8-64 chars, upper+lower+digit, alnum only
  set_env REDSHIFT_PASSWORD "$PW"
  $AWS redshift-serverless create-namespace --namespace-name "$RS_NAMESPACE" \
    --admin-username admin --admin-user-password "$PW" --db-name dev \
    --tags "$TAGS_RS" --query namespace.status --output text
fi
wait_for AVAILABLE $AWS redshift-serverless get-namespace --namespace-name "$RS_NAMESPACE" \
  --query namespace.status --output text

log "Workgroup $RS_WORKGROUP (base 4 RPU, max 4 RPU, public)"
if $AWS redshift-serverless get-workgroup --workgroup-name "$RS_WORKGROUP" >/dev/null 2>&1; then
  echo "exists"
else
  # shellcheck disable=SC2086
  $AWS redshift-serverless create-workgroup --workgroup-name "$RS_WORKGROUP" \
    --namespace-name "$RS_NAMESPACE" \
    --base-capacity 4 --max-capacity 4 \
    --publicly-accessible \
    --security-group-ids "$SG" --subnet-ids $SUBNETS \
    --tags "$TAGS_RS" --query workgroup.status --output text
fi
wait_for AVAILABLE $AWS redshift-serverless get-workgroup --workgroup-name "$RS_WORKGROUP" \
  --query workgroup.status --output text

log "Usage limit: 1 RPU-hour/day, deactivate on breach"
WG_ARN=$($AWS redshift-serverless get-workgroup --workgroup-name "$RS_WORKGROUP" --query workgroup.workgroupArn --output text)
N=$($AWS redshift-serverless list-usage-limits --resource-arn "$WG_ARN" \
  --query "length(usageLimits[?usageType=='serverless-compute'])" --output text)
if [[ $N == 0 ]]; then
  $AWS redshift-serverless create-usage-limit --resource-arn "$WG_ARN" \
    --usage-type serverless-compute --amount 1 --period daily --breach-action deactivate \
    --query 'usageLimit.[usageLimitId,amount,period,breachAction]' --output text
else
  echo "usage limit already present"
fi

HOST=$($AWS redshift-serverless get-workgroup --workgroup-name "$RS_WORKGROUP" --query workgroup.endpoint.address --output text)
set_env REDSHIFT_HOST "$HOST"
set_env REDSHIFT_PORT 5439
set_env REDSHIFT_DB dev
set_env REDSHIFT_USER admin
[[ -n $PW ]] || echo "WARNING: REDSHIFT_PASSWORD missing from $ENV_FILE (namespace pre-existed)"

log "Public IPv4 addresses on the workgroup endpoint (\$0.005/hr each)"
ENIS=$($AWS redshift-serverless get-workgroup --workgroup-name "$RS_WORKGROUP" \
  --query 'workgroup.endpoint.vpcEndpoints[].networkInterfaces[].networkInterfaceId' --output text)
# shellcheck disable=SC2086
[[ -n $ENIS && $ENIS != None ]] && $AWS ec2 describe-network-interfaces --network-interface-ids $ENIS \
  --query 'NetworkInterfaces[].[NetworkInterfaceId,AvailabilityZone,PrivateIpAddress,Association.PublicIp]' --output table
echo "All public IPv4s on ENIs using our SG (Aurora + Redshift):"
$AWS ec2 describe-network-interfaces --filters "Name=group-id,Values=$SG" \
  --query 'NetworkInterfaces[?Association.PublicIp].[NetworkInterfaceId,Description,Association.PublicIp]' --output table
echo "Elastic IPs in the account/region:"
$AWS ec2 describe-addresses --query 'Addresses[].[PublicIp,AllocationId,NetworkInterfaceId,Tags[?Key==`Name`].Value|[0]]' --output table
echo "Redshift ready: $HOST:5439/dev (credentials in $ENV_FILE)"
