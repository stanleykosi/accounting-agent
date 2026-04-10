#!/bin/sh
# Purpose: Create the canonical MinIO buckets required by the local demo stack.
# Scope: Wait for MinIO to become reachable, configure an administrative alias, and create private buckets idempotently.
# Dependencies: Runs inside the minio/mc image and relies on the MINIO_* and ACCOUNTING_AGENT_STORAGE__* environment variables provided by Docker Compose.

set -eu

log() {
  printf '[minio-init] %s\n' "$1"
}

require_env() {
  variable_name="$1"
  variable_value="$(printenv "$variable_name" || true)"
  if [ -z "$variable_value" ]; then
    log "Missing required environment variable: $variable_name"
    exit 1
  fi
}

wait_for_minio() {
  while ! mc alias set local "$MINIO_ENDPOINT_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
    log "Waiting for MinIO at $MINIO_ENDPOINT_URL ..."
    sleep 2
  done
}

create_bucket() {
  bucket_name="$1"
  if mc ls "local/$bucket_name" >/dev/null 2>&1; then
    log "Bucket already present: $bucket_name"
  else
    log "Creating bucket: $bucket_name"
    mc mb "local/$bucket_name"
  fi
  mc anonymous set none "local/$bucket_name" >/dev/null
}

require_env MINIO_ENDPOINT_URL
require_env MINIO_ROOT_USER
require_env MINIO_ROOT_PASSWORD
require_env ACCOUNTING_AGENT_STORAGE__DOCUMENT_BUCKET
require_env ACCOUNTING_AGENT_STORAGE__ARTIFACT_BUCKET
require_env ACCOUNTING_AGENT_STORAGE__DERIVATIVE_BUCKET

wait_for_minio

create_bucket "$ACCOUNTING_AGENT_STORAGE__DOCUMENT_BUCKET"
create_bucket "$ACCOUNTING_AGENT_STORAGE__ARTIFACT_BUCKET"
create_bucket "$ACCOUNTING_AGENT_STORAGE__DERIVATIVE_BUCKET"

log "MinIO bucket initialization complete."
