#!/usr/bin/env bash
# Purpose: Provide shared operator helpers for the canonical local demo scripts.
# Scope: Centralize path resolution, prerequisite checks, Docker Compose access, health waits, and fail-fast validations.
# Dependencies: Bash, Docker Compose, curl, and the repository structure established by the implementation plan.

set -euo pipefail

LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${LIB_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/docker/docker-compose.yml"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE_FILE="${REPO_ROOT}/.env.example"
ALEMBIC_CONFIG_FILE="${REPO_ROOT}/infra/alembic.ini"
BUILD_ROOT="${REPO_ROOT}/build"
BACKUP_ROOT="${BUILD_ROOT}/demo-backups"
DEFAULT_WAIT_TIMEOUT_SECONDS=120

# Emit a consistent script-scoped log line so operator output stays easy to scan.
log() {
  printf '[%s] %s\n' "$(basename "$0")" "$*"
}

# Stop execution immediately with one clear recovery-oriented message.
fail() {
  log "ERROR: $*"
  if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    return 1
  fi
  exit 1
}

# Require a host command before the operator path continues.
require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    fail "Missing required command: ${command_name}. Install it and rerun this script."
  fi
}

# Require a repository file that later steps may still need to add.
require_file() {
  local file_path="$1"
  local recovery_message="$2"

  if [[ ! -f "${file_path}" ]]; then
    fail "${recovery_message}"
  fi
}

# Ensure the Docker daemon is reachable before any Compose-based action begins.
require_docker_daemon() {
  require_command docker

  if ! docker info >/dev/null 2>&1; then
    fail "Docker is installed but the daemon is not reachable. Start Docker Desktop or the Docker service and retry."
  fi
}

# Route all Compose calls through one canonical compose file and project directory.
docker_compose() {
  docker compose --file "${COMPOSE_FILE}" --project-directory "${REPO_ROOT}" "$@"
}

# Create the local runtime directories used by backup and restore flows.
ensure_build_directories() {
  mkdir -p "${BACKUP_ROOT}"
}

# Create `.env` from the canonical example on first bootstrap.
ensure_env_file() {
  require_file \
    "${ENV_EXAMPLE_FILE}" \
    "The repository is missing .env.example. Restore the root environment template before bootstrapping."

  if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${ENV_EXAMPLE_FILE}" "${ENV_FILE}"
    log "Created ${ENV_FILE} from .env.example."
  fi
}

# Load repository environment variables into the current shell for host-side commands.
load_env_file() {
  require_file \
    "${ENV_FILE}" \
    "The local environment file is missing. Run infra/scripts/bootstrap-demo.sh first."

  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

# Wait for a service with a Docker healthcheck to report healthy.
wait_for_service_health() {
  local service_name="$1"
  local timeout_seconds="${2:-${DEFAULT_WAIT_TIMEOUT_SECONDS}}"
  local container_id=""
  local deadline=$((SECONDS + timeout_seconds))

  while [[ ${SECONDS} -lt ${deadline} ]]; do
    container_id="$(docker_compose ps -q "${service_name}")"
    if [[ -n "${container_id}" ]]; then
      local health_status
      health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
      if [[ "${health_status}" == "healthy" ]]; then
        return 0
      fi
      if [[ "${health_status}" == "exited" ]]; then
        fail "Service ${service_name} exited unexpectedly. Inspect 'docker compose logs ${service_name}' for details."
      fi
    fi
    sleep 2
  done

  fail "Timed out waiting for service ${service_name} to become healthy."
}

# Wait for an HTTP endpoint to return a success response.
wait_for_http_ok() {
  local url="$1"
  local timeout_seconds="${2:-${DEFAULT_WAIT_TIMEOUT_SECONDS}}"
  local deadline=$((SECONDS + timeout_seconds))

  require_command curl

  while [[ ${SECONDS} -lt ${deadline} ]]; do
    if curl --silent --show-error --fail "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  fail "Timed out waiting for ${url} to become reachable."
}

# Enforce the single migration path that later steps must populate.
require_migration_assets() {
  require_file \
    "${ALEMBIC_CONFIG_FILE}" \
    "The canonical Alembic config is missing at infra/alembic.ini. Restore it before running database migrations."
  require_file \
    "${REPO_ROOT}/infra/alembic/env.py" \
    "Database migrations are not implemented yet. Add infra/alembic/env.py in Step 8 before starting the demo stack."

  if ! compgen -G "${REPO_ROOT}/infra/alembic/versions/*.py" >/dev/null; then
    fail "Database migrations are not implemented yet. Add the baseline revision under infra/alembic/versions in Step 8 before starting the demo stack."
  fi
}

# Run the canonical Alembic upgrade path from the repository root.
run_database_migrations() {
  require_command uv
  load_env_file
  require_migration_assets

  log "Applying database migrations."
  (
    cd "${REPO_ROOT}"
    uv run alembic -c "${ALEMBIC_CONFIG_FILE}" upgrade head
  )
}

# Require the future demo-data seed entrypoint once fixture assets are added.
require_demo_seed_assets() {
  require_file \
    "${REPO_ROOT}/packages/demo-data/seed.py" \
    "Demo seed assets are not implemented yet. Add packages/demo-data/seed.py in the later demo-data step before seeding the environment."
}

# Run the one canonical MinIO bucket initializer declared in Docker Compose.
run_minio_bucket_init() {
  log "Ensuring MinIO buckets exist."
  docker_compose up --no-deps minio-init
}

# Generate a stable UTC backup identifier for repeatable operator artifacts.
timestamp_utc() {
  date -u '+%Y%m%dT%H%M%SZ'
}
