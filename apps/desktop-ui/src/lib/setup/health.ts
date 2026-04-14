/*
Purpose: Evaluate the local runtime dependencies required before the desktop workspace can enter the main workflow UI.
Scope: Loopback HTTP and TCP health checks for the API, MinIO, PostgreSQL, and Redis plus operator recovery guidance.
Dependencies: Node.js networking APIs, local environment variables, and the shared setup health contracts.
*/

import net from "node:net";
import type {
  DesktopSetupHealthSnapshot,
  LocalServiceHealthCheck,
  LocalServiceHealthStatus,
} from "./types";
import { resolveFrontendRuntimeMode } from "../runtime";

const HEALTHCHECK_TIMEOUT_MS = 2_500;
const RECOVERY_COMMANDS = [
  "./infra/scripts/bootstrap-demo.sh",
  "./infra/scripts/start-demo.sh",
  "./infra/scripts/healthcheck-demo.sh",
] as const;

/**
 * Purpose: Read the canonical local-runtime readiness snapshot for the desktop setup flow.
 * Inputs: None.
 * Outputs: A typed health summary for the critical loopback services that must be reachable before the main UI opens.
 * Behavior: Runs all checks in parallel and fails closed when any dependency is unreachable.
 */
export async function readDesktopSetupHealth(): Promise<DesktopSetupHealthSnapshot> {
  const mode = resolveFrontendRuntimeMode();
  if (mode === "hosted") {
    return {
      checkedAt: new Date().toISOString(),
      mode,
      ready: true,
      recoveryCommands: [],
      services: [],
    };
  }

  const [apiHealth, minioHealth, postgresHealth, redisHealth] = await Promise.all([
    checkHttpService({
      endpoint: resolveApiHealthUrl(),
      failureDetail:
        "The FastAPI service is not reachable on loopback. Start the demo stack and retry.",
      id: "api",
      label: "FastAPI application server",
      successDetail: "The canonical accounting API responded successfully.",
    }),
    checkHttpService({
      endpoint: resolveMinioHealthUrl(),
      failureDetail:
        "MinIO is not reachable. The document, artifact, and derivative buckets must be available before the desktop shell proceeds.",
      id: "minio",
      label: "MinIO object storage",
      successDetail: "MinIO responded to the canonical liveness probe.",
    }),
    checkTcpService({
      failureDetail:
        "PostgreSQL is unreachable. Apply migrations and start the database before using the desktop client.",
      host: process.env.database_host ?? "127.0.0.1",
      id: "postgres",
      label: "PostgreSQL database",
      port: parseIntegerEnv("database_port", 5432),
      successDetail: "The database port accepted a loopback connection.",
    }),
    checkTcpService({
      failureDetail:
        "Redis is unreachable. Background-job dispatch and caching remain blocked until Redis accepts connections.",
      host: resolveRedisHost(),
      id: "redis",
      label: "Redis broker",
      port: resolveRedisPort(),
      successDetail: "The Redis broker accepted a loopback connection.",
    }),
  ]);

  const services = [apiHealth, minioHealth, postgresHealth, redisHealth] as const;

  return {
    checkedAt: new Date().toISOString(),
    mode,
    ready: services.every((service) => service.status === "healthy"),
    recoveryCommands: RECOVERY_COMMANDS,
    services,
  };
}

async function checkHttpService(input: {
  endpoint: string;
  failureDetail: string;
  id: LocalServiceHealthCheck["id"];
  label: string;
  successDetail: string;
}): Promise<LocalServiceHealthCheck> {
  const startedAt = performance.now();

  try {
    const response = await fetch(input.endpoint, {
      cache: "no-store",
      signal: AbortSignal.timeout(HEALTHCHECK_TIMEOUT_MS),
    });
    if (!response.ok) {
      return buildServiceHealth({
        detail: `${input.failureDetail} The endpoint returned HTTP ${response.status}.`,
        endpoint: input.endpoint,
        id: input.id,
        label: input.label,
        latencyMs: roundLatency(performance.now() - startedAt),
        status: "unhealthy",
      });
    }

    return buildServiceHealth({
      detail: input.successDetail,
      endpoint: input.endpoint,
      id: input.id,
      label: input.label,
      latencyMs: roundLatency(performance.now() - startedAt),
      status: "healthy",
    });
  } catch (error: unknown) {
    return buildServiceHealth({
      detail: `${input.failureDetail} ${formatErrorMessage(error)}`,
      endpoint: input.endpoint,
      id: input.id,
      label: input.label,
      latencyMs: null,
      status: "unhealthy",
    });
  }
}

async function checkTcpService(input: {
  failureDetail: string;
  host: string;
  id: LocalServiceHealthCheck["id"];
  label: string;
  port: number;
  successDetail: string;
}): Promise<LocalServiceHealthCheck> {
  const startedAt = performance.now();

  try {
    await new Promise<void>((resolve, reject) => {
      const socket = net.createConnection({
        host: input.host,
        port: input.port,
      });

      socket.setTimeout(HEALTHCHECK_TIMEOUT_MS);
      socket.once("connect", () => {
        socket.end();
        resolve();
      });
      socket.once("timeout", () => {
        socket.destroy();
        reject(new Error("Timed out while opening the TCP socket."));
      });
      socket.once("error", (error) => {
        socket.destroy();
        reject(error);
      });
    });

    return buildServiceHealth({
      detail: input.successDetail,
      endpoint: `${input.host}:${input.port}`,
      id: input.id,
      label: input.label,
      latencyMs: roundLatency(performance.now() - startedAt),
      status: "healthy",
    });
  } catch (error: unknown) {
    return buildServiceHealth({
      detail: `${input.failureDetail} ${formatErrorMessage(error)}`,
      endpoint: `${input.host}:${input.port}`,
      id: input.id,
      label: input.label,
      latencyMs: null,
      status: "unhealthy",
    });
  }
}

function buildServiceHealth(input: {
  detail: string;
  endpoint: string;
  id: LocalServiceHealthCheck["id"];
  label: string;
  latencyMs: number | null;
  status: LocalServiceHealthStatus;
}): LocalServiceHealthCheck {
  return {
    detail: input.detail,
    endpoint: input.endpoint,
    id: input.id,
    label: input.label,
    latencyMs: input.latencyMs,
    status: input.status,
  };
}

function resolveApiHealthUrl(): string {
  const apiBaseUrl = (process.env.ACCOUNTING_AGENT_API_URL ?? "http://127.0.0.1:8000/api").replace(
    /\/+$/u,
    "",
  );
  return `${apiBaseUrl}/health`;
}

function resolveMinioHealthUrl(): string {
  const isSecure = (process.env.storage_secure ?? "false").toLowerCase() === "true";
  const protocol = isSecure ? "https" : "http";
  const endpoint = (process.env.storage_endpoint ?? "127.0.0.1:9000").replace(/\/+$/u, "");
  return `${protocol}://${endpoint}/minio/health/live`;
}

function resolveRedisHost(): string {
  const brokerUrl = process.env.redis_broker_url ?? "redis://127.0.0.1:6379/0";

  try {
    return new URL(brokerUrl).hostname;
  } catch {
    return "127.0.0.1";
  }
}

function resolveRedisPort(): number {
  const brokerUrl = process.env.redis_broker_url ?? "redis://127.0.0.1:6379/0";

  try {
    const parsedUrl = new URL(brokerUrl);
    return Number.parseInt(parsedUrl.port, 10) || 6379;
  } catch {
    return 6379;
  }
}

function parseIntegerEnv(name: string, fallbackValue: number): number {
  const rawValue = process.env[name];
  if (rawValue === undefined) {
    return fallbackValue;
  }

  const parsedValue = Number.parseInt(rawValue, 10);
  return Number.isFinite(parsedValue) ? parsedValue : fallbackValue;
}

function roundLatency(value: number): number {
  return Math.max(1, Math.round(value));
}

function formatErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }

  return "The runtime check failed with an unknown error.";
}
