#!/usr/bin/env bash
# Purpose: Start the canonical local demo stack with dependency validation and database migration gating.
# Scope: Validate bootstrap prerequisites, start infrastructure services, initialize MinIO buckets, apply migrations, launch API and worker containers, and verify health.
# Dependencies: `infra/scripts/_lib.sh`, Docker Compose services in `infra/docker/docker-compose.yml`, `uv`, and the Step 8 Alembic baseline.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# Print command usage for operator discovery.
usage() {
  cat <<'EOF'
Usage: infra/scripts/start-demo.sh

Start the canonical backend demo stack by:
  1. validating bootstrap output and migration assets
  2. starting PostgreSQL, Redis, MinIO, and OpenTelemetry
  3. initializing MinIO buckets
  4. applying Alembic migrations
  5. starting the API and worker services
  6. running the demo healthcheck
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_docker_daemon
require_command uv
ensure_env_file
load_env_file
require_migration_assets

log "Starting infrastructure services."
docker_compose up -d postgres redis minio otel-collector
wait_for_service_health postgres
wait_for_service_health redis
wait_for_http_ok "http://127.0.0.1:9000/minio/health/live"

run_minio_bucket_init
run_database_migrations

log "Starting application services."
docker_compose up -d api worker
wait_for_service_health api
wait_for_service_health worker

"${SCRIPT_DIR}/healthcheck-demo.sh"

log "Demo stack is up."
log "API: http://127.0.0.1:${api_port:-8000}${runtime_api_base_path:-/api}"
log "MinIO Console: http://127.0.0.1:9001"
