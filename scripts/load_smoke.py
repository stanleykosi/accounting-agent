"""
Purpose: Run a lightweight latency smoke against a deployed Accounting AI Agent API.
Scope: Health/readiness probes and concurrent health endpoint requests for release gates.
Dependencies: Python standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from math import ceil
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL_ENV = "PRODUCTION_GATE_API_BASE_URL"
DEFAULT_HEALTH_PATH = "/api/health"
DEFAULT_READY_PATH = "/api/ready"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Capture one HTTP probe outcome."""

    ok: bool
    status_code: int | None
    elapsed_ms: float
    error: str | None
    payload: dict[str, Any] | None = None


def main(argv: list[str] | None = None) -> int:
    """Run the deployed API latency smoke."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv(DEFAULT_BASE_URL_ENV),
        help=f"Base API URL. Defaults to ${DEFAULT_BASE_URL_ENV}.",
    )
    parser.add_argument(
        "--path",
        default=os.getenv("PRODUCTION_GATE_HEALTH_PATH", DEFAULT_HEALTH_PATH),
        help="Endpoint used for repeated latency probes.",
    )
    parser.add_argument(
        "--ready-path",
        default=os.getenv("PRODUCTION_GATE_READY_PATH", DEFAULT_READY_PATH),
        help="Readiness endpoint checked once before the latency run. Use an empty value to skip.",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=int(os.getenv("PRODUCTION_GATE_LOAD_REQUESTS", "24")),
        help="Number of latency probes to send.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("PRODUCTION_GATE_LOAD_CONCURRENCY", "4")),
        help="Maximum concurrent probes.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.getenv("PRODUCTION_GATE_LOAD_TIMEOUT_SECONDS", "3")),
        help="Per-request timeout.",
    )
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=float(os.getenv("PRODUCTION_GATE_MAX_P95_MS", "500")),
        help="Fail when health endpoint p95 latency exceeds this threshold.",
    )
    parser.add_argument(
        "--required",
        action="store_true",
        help="Fail instead of skipping when no base URL is configured.",
    )
    args = parser.parse_args(argv)

    if not args.base_url:
        message = (
            f"[load-smoke] skipped: set {DEFAULT_BASE_URL_ENV} to run deployed latency checks."
        )
        print(message)
        return 1 if args.required else 0

    request_count = _positive_int(args.requests, "--requests")
    concurrency = min(_positive_int(args.concurrency, "--concurrency"), request_count)
    timeout_seconds = _positive_float(args.timeout_seconds, "--timeout-seconds")
    max_p95_ms = _positive_float(args.max_p95_ms, "--max-p95-ms")

    health_url = _join_url(args.base_url, args.path)
    ready_path = str(args.ready_path or "").strip()

    print(f"[load-smoke] probing {health_url}")
    health_probe = _fetch_json(health_url, timeout_seconds=timeout_seconds)
    if not health_probe.ok:
        _print_probe_failure("health", health_probe)
        return 1
    if health_probe.payload and health_probe.payload.get("status") != "ok":
        print(f"[load-smoke] health payload did not report ok: {health_probe.payload}")
        return 1

    if ready_path:
        ready_url = _join_url(args.base_url, ready_path)
        print(f"[load-smoke] probing {ready_url}")
        ready_probe = _fetch_json(ready_url, timeout_seconds=timeout_seconds)
        if not ready_probe.ok:
            _print_probe_failure("readiness", ready_probe)
            return 1
        if ready_probe.payload and ready_probe.payload.get("ready") is not True:
            print(f"[load-smoke] readiness payload did not report ready: {ready_probe.payload}")
            return 1

    started_at = time.monotonic()
    results: list[ProbeResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(_fetch_json, health_url, timeout_seconds=timeout_seconds)
            for _ in range(request_count)
        ]
        for future in as_completed(futures):
            results.append(future.result())

    total_elapsed_ms = (time.monotonic() - started_at) * 1000
    failures = [result for result in results if not result.ok]
    latencies = [result.elapsed_ms for result in results if result.ok]
    if failures:
        print(f"[load-smoke] failed: {len(failures)} of {request_count} probes failed")
        for failure in failures[:3]:
            _print_probe_failure("latency", failure)
        return 1

    p50_ms = _percentile(latencies, 50)
    p95_ms = _percentile(latencies, 95)
    print(
        "[load-smoke] "
        f"requests={request_count} concurrency={concurrency} "
        f"p50_ms={p50_ms:.1f} p95_ms={p95_ms:.1f} total_ms={total_elapsed_ms:.1f}"
    )
    if p95_ms > max_p95_ms:
        print(
            f"[load-smoke] failed: p95 {p95_ms:.1f}ms exceeds threshold {max_p95_ms:.1f}ms"
        )
        return 1

    return 0


def _fetch_json(url: str, *, timeout_seconds: float) -> ProbeResult:
    started_at = time.monotonic()
    request = Request(url, headers={"accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload_bytes = response.read()
            elapsed_ms = (time.monotonic() - started_at) * 1000
            status_code = response.status
    except HTTPError as error:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        return ProbeResult(
            ok=False,
            status_code=error.code,
            elapsed_ms=elapsed_ms,
            error=str(error),
        )
    except URLError as error:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        return ProbeResult(
            ok=False,
            status_code=None,
            elapsed_ms=elapsed_ms,
            error=str(error.reason),
        )
    except TimeoutError as error:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        return ProbeResult(
            ok=False,
            status_code=None,
            elapsed_ms=elapsed_ms,
            error=str(error),
        )

    payload = _decode_json_payload(payload_bytes)
    return ProbeResult(
        ok=200 <= status_code < 300,
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        error=None,
        payload=payload,
    )


def _decode_json_payload(payload_bytes: bytes) -> dict[str, Any] | None:
    if not payload_bytes:
        return None
    parsed = json.loads(payload_bytes.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else None


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    if percentile <= 0:
        return min(values)
    if percentile >= 100:
        return max(values)
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil((percentile / 100) * len(ordered)) - 1))
    return ordered[index]


def _positive_int(value: int, label: str) -> int:
    if value <= 0:
        raise SystemExit(f"{label} must be greater than zero")
    return value


def _positive_float(value: float, label: str) -> float:
    if value <= 0:
        raise SystemExit(f"{label} must be greater than zero")
    return value


def _print_probe_failure(label: str, result: ProbeResult) -> None:
    print(
        f"[load-smoke] {label} probe failed: "
        f"status={result.status_code} elapsed_ms={result.elapsed_ms:.1f} error={result.error}"
    )


if __name__ == "__main__":
    sys.exit(main())
