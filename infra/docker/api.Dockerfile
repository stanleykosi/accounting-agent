# Purpose: Build the canonical API container image for the local demo stack.
# Scope: Install Python dependencies from the workspace pyproject, copy the API and shared backend code, and launch the FastAPI server.
# Dependencies: Docker Compose uses this image definition together with pyproject.toml and the source files under apps/api and services/.

FROM python:3.12-slim

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/workspace
ENV PATH=/workspace/.venv/bin:${PATH}
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1
ENV UV_CACHE_DIR=/home/appuser/.cache/uv
ENV HOME=/home/appuser

WORKDIR /workspace

RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m ensurepip --upgrade \
    && python -m pip install --no-cache-dir --upgrade pip uv

RUN groupadd --system appuser \
    && useradd --system --gid appuser --create-home --home-dir /home/appuser appuser \
    && chown appuser:appuser /workspace

USER appuser

COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && python -m uvicorn --version

COPY --chown=appuser:appuser apps/api ./apps/api
COPY --chown=appuser:appuser services ./services
COPY --chown=appuser:appuser infra/alembic ./infra/alembic
COPY --chown=appuser:appuser infra/alembic.ini ./infra/alembic.ini
COPY --chown=appuser:appuser .env.example ./.env.example

EXPOSE 8000

CMD ["/bin/sh", "-lc", "/workspace/.venv/bin/python -m uvicorn apps.api.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
