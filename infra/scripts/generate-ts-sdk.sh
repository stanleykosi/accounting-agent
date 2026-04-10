#!/usr/bin/env bash
# Purpose: Generate the canonical TypeScript SDK directly from the FastAPI OpenAPI schema.
# Scope: Export the live schema from the Python app, generate OpenAPI TypeScript types, and
# write the tracked SDK artifact that contract drift checks compare against.
# Dependencies: uv, pnpm, apps/api/app/main.py, and the @accounting-ai-agent/ts-sdk package.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
TEMP_DIR="$(mktemp -d)"
OPENAPI_JSON_PATH="${TEMP_DIR}/openapi.json"
GENERATED_TYPES_PATH="${TEMP_DIR}/openapi.ts"
FINAL_TYPES_PATH="${REPO_ROOT}/packages/ts-sdk/src/generated/openapi.ts"

cleanup() {
  rm -rf "${TEMP_DIR}"
}

trap cleanup EXIT

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to generate the TypeScript SDK." >&2
  exit 1
fi

if ! command -v pnpm >/dev/null 2>&1; then
  echo "pnpm is required to generate the TypeScript SDK." >&2
  exit 1
fi

mkdir -p "$(dirname "${FINAL_TYPES_PATH}")"

(
  cd "${REPO_ROOT}"
  OPENAPI_JSON_PATH="${OPENAPI_JSON_PATH}" uv run python - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

from apps.api.app.main import app

output_path = Path(os.environ["OPENAPI_JSON_PATH"])
openapi_schema = app.openapi()
output_path.write_text(
    json.dumps(openapi_schema, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  pnpm --filter @accounting-ai-agent/ts-sdk exec openapi-typescript "${OPENAPI_JSON_PATH}" --output "${GENERATED_TYPES_PATH}"
)

{
  printf '/*\n'
  printf 'Purpose: Provide generated OpenAPI TypeScript types for the Accounting AI Agent API.\n'
  printf 'Scope: Derived from apps/api/app/main.py via infra/scripts/generate-ts-sdk.sh; do not edit manually.\n'
  printf 'Dependencies: FastAPI OpenAPI schema output and the openapi-typescript generator.\n'
  printf '*/\n\n'
  cat "${GENERATED_TYPES_PATH}"
} > "${FINAL_TYPES_PATH}"

(
  cd "${REPO_ROOT}"
  pnpm exec prettier --write "${FINAL_TYPES_PATH}" >/dev/null
)
