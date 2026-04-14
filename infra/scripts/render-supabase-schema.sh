#!/usr/bin/env bash
# Purpose: Render the canonical PostgreSQL schema into one Supabase SQL Editor bootstrap file.
# Scope: Offline Alembic SQL generation for the current migration chain without requiring a live database.
# Dependencies: `uv`, `infra/alembic.ini`, and the repository Python environment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_PATH="${REPO_ROOT}/infra/sql/supabase-bootstrap.sql"
DEFAULT_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5432/accounting_agent"

mkdir -p "$(dirname "${OUTPUT_PATH}")"

cd "${REPO_ROOT}"
DATABASE_URL="${DATABASE_URL:-${DEFAULT_DATABASE_URL}}" \
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}" \
uv run alembic -c infra/alembic.ini upgrade head --sql > "${OUTPUT_PATH}"

printf 'Rendered %s\n' "${OUTPUT_PATH}"
