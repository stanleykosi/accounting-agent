#!/usr/bin/env bash
# Purpose: Load the canonical demo fixtures into a healthy local environment once seed assets exist.
# Scope: Validate runtime health, require the single demo-data seed entrypoint, and execute it through uv.
# Dependencies: `infra/scripts/_lib.sh`, `uv`, the running demo stack, and `packages/demo-data/seed.py`.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

# Print command usage for operator discovery.
usage() {
  cat <<'EOF'
Usage: infra/scripts/seed-demo.sh

Seed the local demo environment by:
  1. verifying the stack health
  2. requiring the canonical demo-data seed script
  3. executing the seed loader with uv
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_command uv
ensure_env_file
require_demo_seed_assets

"${SCRIPT_DIR}/healthcheck-demo.sh"
load_env_file

log "Loading demo seed data."
(
  cd "${REPO_ROOT}"
  uv run python "${REPO_ROOT}/packages/demo-data/seed.py"
)

log "Demo seed completed."
