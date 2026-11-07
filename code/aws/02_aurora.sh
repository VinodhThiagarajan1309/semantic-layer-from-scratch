#!/usr/bin/env bash
# Aurora PostgreSQL Serverless v2 (0-1 ACU, auto-pause after 5 min idle),
# Standard storage, one public db.serverless instance. Writes AURORA_* to .env.aws.
set -euo pipefail
source "$(dirname "$0")/config.sh"
VPC=$(default_vpc); SG=$(sg_id "$VPC")
[[ $SG != None ]] || { echo "Run 01_network.sh first" >&2; exit 1; }

log "Aurora cluster $AURORA_CLUSTER (aurora-postgresql $AURORA_ENGINE_VERSION)"
if $AWS rds describe-db-clusters --db-cluster-identifier "$AURORA_CLUSTER" >/dev/null 2>&1; then
  echo "exists; keeping the password already in $ENV_FILE"
  PW=$(get_env AURORA_PASSWORD)
else
  PW=$(gen_password 24)
  set_env AURORA_PASSWORD "$PW"   # save before create so it is never lost
  $AWS rds create-db-cluster \
    --db-cluster-identifier "$AURORA_CLUSTER" \
    --engine aurora-postgresql --engine-version "$AURORA_ENGINE_VERSION" \
    --serverless-v2-scaling-configuration MinCapacity=0,MaxCapacity=1,SecondsUntilAutoPause=300 \
    --storage-type aurora \
    --master-username c360admin --master-user-password "$PW" --no-manage-master-user-password \
    --database-name c360 \
    --backup-retention-period 1 \
    --no-deletion-protection \
    --no-enable-iam-database-authentication \
    --db-subnet-group-name "$DB_SUBNET_GROUP" --vpc-security-group-ids "$SG" \
    --tags "$TAGS_RDS" --copy-tags-to-snapshot \
    --query DBCluster.Status --output text
fi

log "Instance $AURORA_INSTANCE (db.serverless, public)"
if $AWS rds describe-db-instances --db-instance-identifier "$AURORA_INSTANCE" >/dev/null 2>&1; then
  echo "exists"
else
  $AWS rds create-db-instance \
    --db-instance-identifier "$AURORA_INSTANCE" --db-cluster-identifier "$AURORA_CLUSTER" \
    --engine aurora-postgresql --db-instance-class db.serverless \
    --publicly-accessible \
    --no-enable-performance-insights --monitoring-interval 0 \
    --no-auto-minor-version-upgrade \
    --tags "$TAGS_RDS" \
    --query DBInstance.DBInstanceStatus --output text
fi

log "Waiting for cluster + instance to become available (~10-15 min)"
$AWS rds wait db-cluster-available --db-cluster-identifier "$AURORA_CLUSTER"
$AWS rds wait db-instance-available --db-instance-identifier "$AURORA_INSTANCE"

HOST=$($AWS rds describe-db-clusters --db-cluster-identifier "$AURORA_CLUSTER" \
  --query 'DBClusters[0].Endpoint' --output text)
set_env AURORA_HOST "$HOST"
set_env AURORA_PORT 5432
set_env AURORA_DB c360
set_env AURORA_USER c360admin
[[ -n $PW ]] || echo "WARNING: AURORA_PASSWORD missing from $ENV_FILE (cluster pre-existed)"
echo "Aurora ready: $HOST:5432/c360 (credentials in $ENV_FILE)"
