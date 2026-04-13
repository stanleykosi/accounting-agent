#!/usr/bin/env bash
# Purpose: Build the standalone desktop UI, stage packaged sidecar resources, and produce a Tauri desktop bundle.
# Scope: Validates host prerequisites, prepares the bundled Node runtime and Next.js standalone server, then invokes the canonical Tauri build without bundling operator secrets.
# Dependencies: `infra/scripts/_lib.sh`, `pnpm`, `node`, `cargo`, `rustc`, apps/desktop-ui standalone output, and apps/desktop-shell/src-tauri/tauri.conf.json.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

DESKTOP_SHELL_ROOT="${REPO_ROOT}/apps/desktop-shell"
STANDALONE_UI_ROOT="${REPO_ROOT}/apps/desktop-ui/.next/standalone/apps/desktop-ui"
STAGED_UI_ROOT="${DESKTOP_SHELL_ROOT}/resources/desktop-ui"
STAGED_RUNTIME_ROOT="${DESKTOP_SHELL_ROOT}/resources/runtime"

usage() {
  cat <<'EOF'
Usage: infra/scripts/package-desktop.sh [tauri build args...]

Package the canonical desktop shell by:
  1. validating Node, pnpm, Rust, and Cargo
  2. building the Next.js standalone desktop UI
  3. staging the standalone server and static assets into desktop-shell resources
  4. copying the host Node runtime into bundled resources
  5. invoking the Tauri build with any forwarded arguments

Examples:
  ./infra/scripts/package-desktop.sh
  ./infra/scripts/package-desktop.sh --debug
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_command node
require_command pnpm
require_command cargo
require_command rustc

stage_desktop_ui_resources() {
  local static_assets_root="${REPO_ROOT}/apps/desktop-ui/.next/static"
  local public_assets_root="${REPO_ROOT}/apps/desktop-ui/public"

  require_file \
    "${STANDALONE_UI_ROOT}/server.js" \
    "The desktop UI standalone server was not built. Run the Next.js build again before packaging the desktop shell."
  require_file \
    "${DESKTOP_SHELL_ROOT}/src-tauri/tauri.conf.json" \
    "The Tauri desktop-shell configuration is missing. Restore apps/desktop-shell/src-tauri/tauri.conf.json before packaging."

  log "Refreshing staged desktop-shell resources."
  find "${STAGED_UI_ROOT}" -mindepth 1 ! -name '.gitkeep' -exec rm -rf {} +
  find "${STAGED_RUNTIME_ROOT}" -mindepth 1 ! -name '.gitkeep' -exec rm -rf {} +

  mkdir -p "${STAGED_UI_ROOT}/.next" "${STAGED_RUNTIME_ROOT}"
  cp -R "${STANDALONE_UI_ROOT}/." "${STAGED_UI_ROOT}/"
  cp -R "${static_assets_root}" "${STAGED_UI_ROOT}/.next/static"

  if [[ -d "${public_assets_root}" ]] && compgen -G "${public_assets_root}/*" >/dev/null; then
    cp -R "${public_assets_root}" "${STAGED_UI_ROOT}/public"
  fi
}

stage_node_runtime() {
  local node_binary_path
  node_binary_path="$(command -v node)"

  log "Copying the host Node runtime into the desktop-shell bundle resources."
  cp "${node_binary_path}" "${STAGED_RUNTIME_ROOT}/$(platform_node_binary_name)"
  chmod +x "${STAGED_RUNTIME_ROOT}/$(platform_node_binary_name)"
}

platform_node_binary_name() {
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*|Windows_NT)
      printf 'node.exe'
      ;;
    *)
      printf 'node'
      ;;
  esac
}

log "Building the standalone desktop UI."
(
  cd "${REPO_ROOT}"
  pnpm --filter @accounting-ai-agent/desktop-ui build
)

stage_desktop_ui_resources
stage_node_runtime

log "Running the Tauri desktop build."
(
  cd "${DESKTOP_SHELL_ROOT}"
  pnpm exec tauri build --config src-tauri/tauri.conf.json "$@"
)

log "Desktop packaging completed."
