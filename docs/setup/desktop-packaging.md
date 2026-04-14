# Desktop Packaging

<!--
Purpose: Document the canonical packaging flow for the Tauri desktop shell in hosted and local-sidecar modes.
Scope: Host prerequisites, staged resource layout, remote frontend boot behavior, optional local sidecar behavior, and operator recovery guidance for desktop installers.
Dependencies: apps/desktop-shell, infra/scripts/package-desktop.sh, and the standalone Next.js desktop UI in apps/desktop-ui.
-->

## Canonical Packaging Path

Build desktop installers with:

```bash
./infra/scripts/package-desktop.sh
```

This is the only supported packaging path. The script fails fast when a prerequisite is missing instead of attempting alternate desktop build flows.

## Host Prerequisites

- `node`
- `pnpm`
- `cargo`
- `rustc`
- the OS-specific Tauri prerequisites for the machine doing the build

## What The Script Does

1. Builds the Next.js desktop UI in standalone mode.
2. Stages the standalone server into `apps/desktop-shell/resources/desktop-ui/`.
3. Copies `.next/static/` into the staged bundle so client assets remain available to the packaged sidecar.
4. Copies the current host `node` binary into `apps/desktop-shell/resources/runtime/`.
5. Runs `tauri build` with `apps/desktop-shell/src-tauri/tauri.conf.json`.

The packaging flow does not bundle the repository `.env` or any other operator secrets into the installer.

At runtime the packaged shell chooses one canonical path:

- if `ACCOUNTING_AGENT_DESKTOP_REMOTE_URL` is set, the shell opens that hosted frontend URL directly
- otherwise, the shell launches the bundled Node runtime and standalone Next.js server on loopback

## Runtime Configuration

The desktop shell passes runtime configuration directly into the bundled Node sidecar through `process.env`. That keeps non-default loopback settings available to the Next.js standalone server without shipping secrets inside the installer.

Supported inputs include:

- `ACCOUNTING_AGENT_DESKTOP_REMOTE_URL`
- `ACCOUNTING_AGENT_FRONTEND_MODE`
- `ACCOUNTING_AGENT_API_URL`
- `ACCOUNTING_AGENT_SESSION_COOKIE_NAME`
- `api_host`
- `api_port`
- `runtime_api_base_path`
- `database_host`
- `database_port`
- `redis_broker_url`
- `storage_endpoint`
- `storage_secure`
- `security_session_cookie_name`

If `ACCOUNTING_AGENT_DESKTOP_REMOTE_URL` is set, the shell skips the local sidecar entirely and opens the hosted frontend. If the remote URL is omitted, the shell uses the bundled standalone Next.js app and derives `ACCOUNTING_AGENT_API_URL` from `api_host`, `api_port`, and `runtime_api_base_path` when needed.

For packaged installs, place optional overrides in `desktop-shell.env` under the Tauri app config directory. Example:

```dotenv
ACCOUNTING_AGENT_DESKTOP_REMOTE_URL=https://app.example.com
```

For local-sidecar desktop packaging instead, use:

```dotenv
ACCOUNTING_AGENT_FRONTEND_MODE=desktop-local
api_host=127.0.0.1
api_port=8010
runtime_api_base_path=/api
redis_broker_url=redis://127.0.0.1:6380/0
storage_endpoint=127.0.0.1:9005
storage_secure=false
```

## First-Run Setup UX

The hosted desktop shell does not use the local setup page. The local setup route only applies when the packaged shell is intentionally running the bundled standalone server on loopback.

The setup route checks these local services before protected workspace routes can open:

- FastAPI on loopback
- MinIO object storage
- PostgreSQL
- Redis

If any check fails, the setup screen stays in place and shows the canonical recovery commands:

```bash
./infra/scripts/bootstrap-demo.sh
./infra/scripts/start-demo.sh
./infra/scripts/healthcheck-demo.sh
```

## Packaging Notes

- Build the installer on the same OS you intend to target.
- The packaging flow bundles the host Node runtime rather than requiring operators to install Node separately.
- The packaged shell can act as a thin native wrapper around the hosted frontend by setting `ACCOUNTING_AGENT_DESKTOP_REMOTE_URL`.
- The packaged shell uses canonical defaults only when no runtime overrides are supplied through environment variables or `desktop-shell.env`.
- The debug shell rebuilds the standalone desktop UI before launch so it does not serve stale or missing output from a prior run.

## Recovery Guidance

- If the script fails before the Tauri build starts, install the missing host prerequisite and rerun the script.
- If the shell cannot find `server.js`, rerun the Next.js standalone build through `package-desktop.sh`.
- If the packaged app opens but stays on the setup screen, it is running in local-sidecar mode. Start the local demo stack with the shared operator scripts and use the in-app refresh button.
- If the packaged app should be hosted-first, verify `ACCOUNTING_AGENT_DESKTOP_REMOTE_URL` points at the deployed frontend origin.
