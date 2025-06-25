#!/bin/bash
set -euo pipefail

# --- CONFIGURATION ---
PG_CONTAINER="redash_postgres_1"
REDIS_CONTAINER="redash_redis_1"
COMPOSE_FILE="/opt/redash/docker-compose.yml"
ENV_FILE="/opt/redash/.env"
ENV_FILE_ALT="/opt/redash/env"
BACKUP_DIR="/opt/backups"

# --- TIMESTAMP & WORKDIR ---
timestamp=$(date -u +"%Y%m%dT%H%M%SZ")
workdir="${BACKUP_DIR}/redash-backup-${timestamp}"
mkdir -p "$workdir"

# --- DETECT REDASH DATABASE NAME ---
echo "Detecting Redash database name..."
databases=$(docker exec "$PG_CONTAINER" psql -U postgres -t -c "SELECT datname FROM pg_database WHERE datistemplate = false;")
redash_db=""
for db in $databases; do
  if [[ "$db" == *redash* ]]; then
    redash_db="$db"
    break
  fi
done
if [[ -z "$redash_db" ]]; then
  # Fallback: look for Redash tables in all databases
  for db in $databases; do
    [[ "$db" == "template0" || "$db" == "template1" ]] && continue
    tables=$(docker exec "$PG_CONTAINER" psql -U postgres -d "$db" -t -c "SELECT tablename FROM pg_tables WHERE schemaname = 'public';")
    for t in queries dashboards users data_sources query_results; do
      if echo "$tables" | grep -q "^$t$"; then
        redash_db="$db"
        break 2
      fi
    done
  done
fi
if [[ -z "$redash_db" ]]; then
  redash_db="postgres"
  echo "No specific Redash DB found, defaulting to 'postgres'"
else
  echo "Detected Redash DB: $redash_db"
fi

# --- BACKUP POSTGRES ---
echo "Backing up all Postgres roles and DBs..."
docker exec "$PG_CONTAINER" pg_dumpall -U postgres > "${workdir}/postgres-${timestamp}.sql"

echo "Backing up Redash DB schema and data..."
docker exec "$PG_CONTAINER" pg_dump -U postgres -d "$redash_db" --clean --create --disable-triggers --no-owner --no-privileges > "${workdir}/redash-db-${timestamp}.sql"

echo "Backing up Redash DB data only..."
docker exec "$PG_CONTAINER" pg_dump -U postgres -d "$redash_db" --data-only --disable-triggers --no-owner --no-privileges > "${workdir}/redash-data-${timestamp}.sql"

# --- BACKUP REDIS ---
echo "Triggering Redis SAVE and copying dump..."
docker exec "$REDIS_CONTAINER" redis-cli SAVE
docker cp "$REDIS_CONTAINER:/data/dump.rdb" "${workdir}/redis-dump.rdb"

# --- BACKUP CONFIG FILES ---
for fn in "$COMPOSE_FILE" "$ENV_FILE" "$ENV_FILE_ALT"; do
  if [[ -f "$fn" ]]; then
    cp "$fn" "$workdir/"
  fi
done

# --- CREATE MANIFEST ---
manifest="${workdir}/backup-manifest.json"
cat > "$manifest" <<EOF
{
  "timestamp": "$timestamp",
  "redash_database": "$redash_db",
  "files": {
    "full_dump": "postgres-${timestamp}.sql",
    "redash_dump": "redash-db-${timestamp}.sql",
    "redash_data": "redash-data-${timestamp}.sql",
    "redis_dump": "redis-dump.rdb"
  }
}
EOF

# --- ZIP IT UP ---
archive="${BACKUP_DIR}/redash-backup-${timestamp}.zip"
cd "$workdir"
zip -r "$archive" ./*
cd /
rm -rf "$workdir"

echo "✅ Backup complete: $archive" 