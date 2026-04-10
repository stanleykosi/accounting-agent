# Purpose: Build the canonical worker container image for the local demo stack.
# Scope: Install Python dependencies from the workspace pyproject, copy the worker and shared backend code, and launch the worker runtime process.
# Dependencies: Docker Compose uses this image definition together with pyproject.toml and the source files under apps/worker and services/.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/workspace
ENV PATH=/workspace/.venv/bin:/root/.local/bin:${PATH}
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1

WORKDIR /workspace

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m ensurepip --upgrade \
    && python -m pip install --no-cache-dir --upgrade pip uv

COPY pyproject.toml README.md ./
RUN uv sync --no-dev --no-install-project

COPY apps/worker ./apps/worker
COPY services ./services
COPY .env.example ./.env.example

CMD ["python", "-m", "apps.worker.app.runtime", "--heartbeat-seconds", "30"]
