# Shared settings for the Stardog c360 sandbox scripts. Sourced, not executed.
# shellcheck shell=bash

REGION=us-west-2
CE_REGION=us-east-1            # Cost Explorer / Budgets are global APIs served from us-east-1
PREFIX=c360-stardog
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

STARDOG_IP=44.238.183.206/32   # Stardog Cloud virtual-graph egress (us-west-2)
MY_IP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]' || true)"
[[ -n $MY_IP ]] && MY_IP="$MY_IP/32"

TAG_KEY=Project
TAG_VAL=stardog-c360
BUDGET_EMAIL="${BUDGET_EMAIL:-you@example.com}"   # where cost-anomaly emails go

# Our own tagged subnets in 2a/2b/2c of the default VPC (AZ IDs usw2-az2/az1/az3).
# Set PROTECTED_SUBNET to a subnet id the scripts must never touch (optional).
PROTECTED_SUBNET="${PROTECTED_SUBNET:-}"
SUBNET_PLAN="us-west-2a:172.31.16.0/20 us-west-2b:172.31.32.0/20 us-west-2c:172.31.48.0/20"

SG_NAME="${PREFIX}-sg"
DB_SUBNET_GROUP="${PREFIX}-dbsubnets"
AURORA_CLUSTER="${PREFIX}-aurora"
AURORA_INSTANCE="${PREFIX}-aurora-1"
AURORA_ENGINE_VERSION=16.14    # latest 16.x (non-limitless) with ServerlessV2 MinCapacity 0, checked 2026-09-22
RS_NAMESPACE="${PREFIX}-ns"
RS_WORKGROUP="${PREFIX}-wg"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/.env.aws"

export AWS_PAGER=""
AWS="aws --region $REGION --no-cli-pager"

# Tag strings in the formats each service expects
TAGS_RDS="Key=$TAG_KEY,Value=$TAG_VAL"
TAGS_RS="key=$TAG_KEY,value=$TAG_VAL"
tagspec() { echo "ResourceType=$1,Tags=[{Key=$TAG_KEY,Value=$TAG_VAL},{Key=Name,Value=${2:-$PREFIX}}]"; }

log() { echo; echo "==> $*"; }

# Alphanumeric password of length $1 (default 24); no quotes, @, /, \ or spaces.
gen_password() {
  local p
  while :; do
    p="$(openssl rand -base64 64 | tr -dc 'A-Za-z0-9')"
    p="${p:0:${1:-24}}"
    [[ $p =~ [A-Z] && $p =~ [a-z] && $p =~ [0-9] ]] && { echo "$p"; return; }
  done
}

# set_env KEY VALUE: add or replace KEY=VALUE in aws/.env.aws, chmod 600.
set_env() {
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  local tmp; tmp="$(mktemp)"
  grep -v "^$1=" "$ENV_FILE" > "$tmp" || true
  echo "$1=$2" >> "$tmp"
  mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}
get_env() { [[ -f $ENV_FILE ]] && grep "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

default_vpc() {
  $AWS ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text
}
# Our tagged subnets (space-separated IDs)
our_subnets() {
  $AWS ec2 describe-subnets --filters "Name=vpc-id,Values=$1" "Name=tag:$TAG_KEY,Values=$TAG_VAL" \
    --query 'Subnets[].SubnetId' --output text
}
sg_id() {
  $AWS ec2 describe-security-groups --filters "Name=vpc-id,Values=$1" "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text
}
