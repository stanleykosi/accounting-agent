#!/usr/bin/env bash
# Purpose: Verify the canonical local demo stack health from the operator boundary.
# Scope: Check the API endpoint, PostgreSQL, Redis, MinIO bucket presence, and worker dependency health.
# Dependencies: `infra/scripts/_lib.sh`, Docker Compose, curl, and the runtime services defined in `infra/docker/docker-compose.yml`.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# Print command usage for operator discovery.
usage() {
  cat <<'EOF'
Usage: infra/scripts/healthcheck-demo.sh

Verify:
  - API health endpoint
  - PostgreSQL query reachability
  - Redis ping
  - MinIO liveness and required buckets
  - worker dependency healthcheck
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_docker_daemon
require_command curl
ensure_env_file
load_env_file

log "Checking API health endpoint."
curl --silent --show-error --fail \
  "http://127.0.0.1:${ACCOUNTING_AGENT_API__PORT}${ACCOUNTING_AGENT_RUNTIME__API_BASE_PATH}/health" \
  >/dev/null

log "Checking PostgreSQL connectivity."
docker_compose exec -T \
  -e "PGPASSWORD=${ACCOUNTING_AGENT_DATABASE__PASSWORD}" \
  postgres \
  psql \
  --username="${ACCOUNTING_AGENT_DATABASE__USER}" \
  --dbname="${ACCOUNTING_AGENT_DATABASE__NAME}" \
  --tuples-only \
  --command="SELECT 1;" \
  >/dev/null

log "Checking Redis connectivity."
docker_compose exec -T redis redis-cli ping | grep -qx 'PONG'

log "Checking MinIO liveness and canonical buckets."
curl --silent --show-error --fail "http://127.0.0.1:9000/minio/health/live" >/dev/null
docker_compose run --rm --no-deps \
  --entrypoint /bin/sh \
  minio-init \
  -lc '
    set -eu
    mc alias set local "$MINIO_ENDPOINT_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc ls "local/$ACCOUNTING_AGENT_STORAGE__DOCUMENT_BUCKET" >/dev/null
    mc ls "local/$ACCOUNTING_AGENT_STORAGE__ARTIFACT_BUCKET" >/dev/null
    mc ls "local/$ACCOUNTING_AGENT_STORAGE__DERIVATIVE_BUCKET" >/dev/null
  ' \
  >/dev/null

log "Checking worker dependency health."
docker_compose exec -T worker python -m apps.worker.app.runtime --healthcheck >/dev/null

log "All demo health checks passed."
