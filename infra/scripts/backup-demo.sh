#!/usr/bin/env bash
# Purpose: Create a portable local backup for the canonical demo environment.
# Scope: Snapshot the PostgreSQL database and mirror MinIO bucket contents plus manifests into a timestamped backup directory.
# Dependencies: `infra/scripts/_lib.sh`, Docker Compose, PostgreSQL tools in the container image, and the MinIO mc image declared by the compose stack.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# Print command usage for operator discovery.
usage() {
  cat <<'EOF'
Usage: infra/scripts/backup-demo.sh [backup-name]

Create a backup under build/demo-backups. When no backup name is supplied,
the script uses a UTC timestamp.
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_docker_daemon
ensure_env_file
ensure_build_directories
load_env_file

backup_name="${1:-$(timestamp_utc)}"
backup_dir="${BACKUP_ROOT}/${backup_name}"
metadata_dir="${backup_dir}/metadata"
objects_dir="${backup_dir}/objects"
database_dump_path="${backup_dir}/database.dump"
manifest_path="${metadata_dir}/backup-manifest.txt"

if [[ -e "${backup_dir}" ]]; then
  fail "Backup target already exists at ${backup_dir}. Choose a different backup name."
fi

mkdir -p "${metadata_dir}" "${objects_dir}"

log "Running demo healthcheck before backup."
"${SCRIPT_DIR}/healthcheck-demo.sh"

log "Dumping PostgreSQL database to ${database_dump_path}."
docker_compose exec -T \
  -e "PGPASSWORD=${database_password}" \
  postgres \
  pg_dump \
  --format=custom \
  --dbname="${database_name}" \
  --username="${database_user}" \
  > "${database_dump_path}"

cat > "${manifest_path}" <<EOF
backup_name=${backup_name}
created_at_utc=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
database_name=${database_name}
document_bucket=${storage_document_bucket}
artifact_bucket=${storage_artifact_bucket}
derivative_bucket=${storage_derivative_bucket}
EOF

log "Mirroring MinIO buckets into ${objects_dir}."
docker_compose run --rm --no-deps \
  -v "${objects_dir}:/backup" \
  --entrypoint /bin/sh \
  minio-init \
  -lc '
    set -eu
    mc alias set local "$MINIO_ENDPOINT_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mkdir -p /backup/documents /backup/artifacts /backup/derivatives /backup/manifests
    mc mirror --overwrite "local/$storage_document_bucket" /backup/documents
    mc mirror --overwrite "local/$storage_artifact_bucket" /backup/artifacts
    mc mirror --overwrite "local/$storage_derivative_bucket" /backup/derivatives
    mc ls --recursive --json "local/$storage_document_bucket" > /backup/manifests/documents.jsonl
    mc ls --recursive --json "local/$storage_artifact_bucket" > /backup/manifests/artifacts.jsonl
    mc ls --recursive --json "local/$storage_derivative_bucket" > /backup/manifests/derivatives.jsonl
  '

log "Backup complete at ${backup_dir}."
