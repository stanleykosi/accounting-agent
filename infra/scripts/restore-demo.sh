#!/usr/bin/env bash
# Purpose: Restore a previously captured local demo backup into the canonical runtime stack.
# Scope: Validate the backup payload, stop write-capable services, restore PostgreSQL, repopulate MinIO buckets, and restart the application services.
# Dependencies: `infra/scripts/_lib.sh`, Docker Compose, PostgreSQL restore tools in the container image, and the MinIO mc image declared by the compose stack.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# Print command usage for operator discovery.
usage() {
  cat <<'EOF'
Usage: infra/scripts/restore-demo.sh <backup-name-or-path>

Restore a backup created by infra/scripts/backup-demo.sh. The argument may be:
  - a directory name under build/demo-backups
  - an absolute path to a backup directory
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

require_docker_daemon
ensure_env_file
load_env_file
require_migration_assets

backup_input="$1"
if [[ "${backup_input}" = /* ]]; then
  backup_dir="${backup_input}"
else
  backup_dir="${BACKUP_ROOT}/${backup_input}"
fi

database_dump_path="${backup_dir}/database.dump"
objects_dir="${backup_dir}/objects"

require_file \
  "${database_dump_path}" \
  "Backup is incomplete. Missing database dump at ${database_dump_path}."
require_file \
  "${objects_dir}/manifests/documents.jsonl" \
  "Backup is incomplete. Missing MinIO manifest for the document bucket."
require_file \
  "${objects_dir}/manifests/artifacts.jsonl" \
  "Backup is incomplete. Missing MinIO manifest for the artifact bucket."
require_file \
  "${objects_dir}/manifests/derivatives.jsonl" \
  "Backup is incomplete. Missing MinIO manifest for the derivative bucket."

log "Starting infrastructure services required for restore."
docker_compose up -d postgres redis minio otel-collector
wait_for_service_health postgres
wait_for_service_health redis
wait_for_http_ok "http://127.0.0.1:9000/minio/health/live"
run_minio_bucket_init

log "Stopping application services to avoid writes during restore."
docker_compose stop api worker >/dev/null 2>&1 || true

log "Restoring PostgreSQL database from ${database_dump_path}."
docker_compose exec -T \
  -e "PGPASSWORD=${ACCOUNTING_AGENT_DATABASE__PASSWORD}" \
  postgres \
  psql \
  --username="${ACCOUNTING_AGENT_DATABASE__USER}" \
  --dbname=postgres \
  --command="DROP DATABASE IF EXISTS ${ACCOUNTING_AGENT_DATABASE__NAME};"
docker_compose exec -T \
  -e "PGPASSWORD=${ACCOUNTING_AGENT_DATABASE__PASSWORD}" \
  postgres \
  psql \
  --username="${ACCOUNTING_AGENT_DATABASE__USER}" \
  --dbname=postgres \
  --command="CREATE DATABASE ${ACCOUNTING_AGENT_DATABASE__NAME};"
docker_compose exec -T \
  -e "PGPASSWORD=${ACCOUNTING_AGENT_DATABASE__PASSWORD}" \
  postgres \
  pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --username="${ACCOUNTING_AGENT_DATABASE__USER}" \
  --dbname="${ACCOUNTING_AGENT_DATABASE__NAME}" \
  < "${database_dump_path}"

log "Restoring MinIO bucket contents from ${objects_dir}."
docker_compose run --rm --no-deps \
  -v "${objects_dir}:/backup" \
  --entrypoint /bin/sh \
  minio-init \
  -lc '
    set -eu
    mc alias set local "$MINIO_ENDPOINT_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc rm --recursive --force "local/$ACCOUNTING_AGENT_STORAGE__DOCUMENT_BUCKET" >/dev/null 2>&1 || true
    mc rm --recursive --force "local/$ACCOUNTING_AGENT_STORAGE__ARTIFACT_BUCKET" >/dev/null 2>&1 || true
    mc rm --recursive --force "local/$ACCOUNTING_AGENT_STORAGE__DERIVATIVE_BUCKET" >/dev/null 2>&1 || true
    mc mirror --overwrite /backup/documents "local/$ACCOUNTING_AGENT_STORAGE__DOCUMENT_BUCKET"
    mc mirror --overwrite /backup/artifacts "local/$ACCOUNTING_AGENT_STORAGE__ARTIFACT_BUCKET"
    mc mirror --overwrite /backup/derivatives "local/$ACCOUNTING_AGENT_STORAGE__DERIVATIVE_BUCKET"
  '

log "Starting application services after restore."
docker_compose up -d api worker
wait_for_service_health api
wait_for_service_health worker
"${SCRIPT_DIR}/healthcheck-demo.sh"

log "Restore complete from ${backup_dir}."
