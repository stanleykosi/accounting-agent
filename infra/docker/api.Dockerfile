# Purpose: Build the canonical API container image for the local demo stack.
# Scope: Install Python dependencies from the workspace pyproject, copy the API and shared backend code, and launch the FastAPI server.
# Dependencies: Docker Compose uses this image definition together with pyproject.toml and the source files under apps/api and services/.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/workspace
ENV PATH=/workspace/.venv/bin:${PATH}
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1
ENV HOME=/home/appuser

WORKDIR /workspace

RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m ensurepip --upgrade \
    && python -m pip install --no-cache-dir --upgrade pip uv

RUN groupadd --system appuser \
    && useradd --system --gid appuser --create-home --home-dir /home/appuser appuser

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && /workspace/.venv/bin/python -m uvicorn --version

COPY apps/api ./apps/api
COPY services ./services
COPY infra/alembic ./infra/alembic
COPY infra/alembic.ini ./infra/alembic.ini
COPY .env.example ./.env.example

RUN chown -R appuser:appuser /workspace /home/appuser

USER appuser

EXPOSE 8000

CMD ["/bin/sh", "-lc", "/workspace/.venv/bin/python -m uvicorn apps.api.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
