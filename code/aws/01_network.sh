#!/usr/bin/env bash
# Network: 3 tagged public subnets in the default VPC (2a/2b/2c), one security
# group (5432 + 5439 from Stardog Cloud and this machine only), and an RDS DB
# subnet group. Idempotent: reuses anything that already exists.
set -euo pipefail
source "$(dirname "$0")/config.sh"
[[ -n $MY_IP ]] || { echo "Could not detect MY_IP via checkip.amazonaws.com" >&2; exit 1; }

VPC=$(default_vpc)
[[ $VPC != None ]] || { echo "No default VPC in $REGION" >&2; exit 1; }
log "Default VPC: $VPC"

# The default VPC's main route table already routes 0.0.0.0/0 to its IGW, and
# new subnets are implicitly associated with the main table. Verify that.
MAIN_RTB=$($AWS ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC" Name=association.main,Values=true \
  --query 'RouteTables[0].RouteTableId' --output text)
IGW_ROUTE=$($AWS ec2 describe-route-tables --route-table-ids "$MAIN_RTB" \
  --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0'].GatewayId | [0]" --output text)
echo "main route table $MAIN_RTB, 0.0.0.0/0 -> $IGW_ROUTE"
[[ $IGW_ROUTE == igw-* ]] || { echo "Main route table has no IGW route; subnets would not be public" >&2; exit 1; }

log "Subnets"
for pair in $SUBNET_PLAN; do
  az=${pair%%:*}; cidr=${pair#*:}
  id=$($AWS ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" "Name=cidr-block,Values=$cidr" \
    --query 'Subnets[0].SubnetId' --output text)
  if [[ $id == None ]]; then
    id=$($AWS ec2 create-subnet --vpc-id "$VPC" --availability-zone "$az" --cidr-block "$cidr" \
      --tag-specifications "$(tagspec subnet "${PREFIX}-$az")" --query Subnet.SubnetId --output text)
    echo "created $id ($az $cidr)"
  else
    echo "reusing $id ($az $cidr)"
  fi
  [[ $id != "$PROTECTED_SUBNET" ]] || { echo "Refusing to use protected subnet $id" >&2; exit 1; }
  $AWS ec2 modify-subnet-attribute --subnet-id "$id" --map-public-ip-on-launch
done
SUBNETS=$(our_subnets "$VPC")
echo "our subnets: $SUBNETS"

log "Security group $SG_NAME"
SG=$(sg_id "$VPC")
if [[ $SG == None ]]; then
  SG=$($AWS ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC" \
    --description "Stardog Cloud VG demo: Postgres/Redshift from Stardog + my IP" \
    --tag-specifications "$(tagspec security-group "$SG_NAME")" --query GroupId --output text)
  echo "created $SG"
else
  echo "reusing $SG"
fi
for port in 5432 5439; do
  for cidr in "$STARDOG_IP" "$MY_IP"; do
    $AWS ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port "$port" --cidr "$cidr" \
      >/dev/null 2>&1 && echo "allowed $cidr -> $port" || echo "rule exists (or failed): $cidr -> $port"
  done
done
$AWS ec2 describe-security-groups --group-ids "$SG" \
  --query 'SecurityGroups[0].IpPermissions[].[FromPort,IpRanges[].CidrIp]' --output text

log "DB subnet group $DB_SUBNET_GROUP"
if $AWS rds describe-db-subnet-groups --db-subnet-group-name "$DB_SUBNET_GROUP" >/dev/null 2>&1; then
  echo "exists"
else
  # shellcheck disable=SC2086
  $AWS rds create-db-subnet-group --db-subnet-group-name "$DB_SUBNET_GROUP" \
    --db-subnet-group-description "c360 Stardog demo (default VPC)" \
    --subnet-ids $SUBNETS --tags "$TAGS_RDS" --query DBSubnetGroup.SubnetGroupStatus --output text
fi
echo "Network ready: VPC=$VPC SG=$SG"
