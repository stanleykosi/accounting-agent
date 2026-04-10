#!/usr/bin/env bash
# Purpose: Bootstrap the canonical local demo environment before the operator starts the stack.
# Scope: Validate host prerequisites, create the local environment file, install workspace dependencies, and prepare backup directories.
# Dependencies: `infra/scripts/_lib.sh`, `uv`, `pnpm`, Docker, `.env.example`, `pyproject.toml`, and `package.json`.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# Print command usage for operator discovery.
usage() {
  cat <<'EOF'
Usage: infra/scripts/bootstrap-demo.sh

Bootstrap the local demo environment by:
  1. validating Docker, uv, and pnpm
  2. creating .env from .env.example if needed
  3. syncing Python dependencies
  4. installing pnpm workspace dependencies
  5. creating the local backup directory
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_command uv
require_command pnpm
require_docker_daemon
ensure_env_file
ensure_build_directories

log "Syncing Python dependencies with uv."
(
  cd "${REPO_ROOT}"
  uv sync
)

log "Installing workspace dependencies with pnpm."
(
  cd "${REPO_ROOT}"
  pnpm install --frozen-lockfile
)

log "Bootstrap complete."
log "Next steps: run infra/scripts/start-demo.sh, then infra/scripts/seed-demo.sh once demo fixtures exist."
