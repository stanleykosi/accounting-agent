# Purpose: Build the canonical API container image for the local demo stack.
# Scope: Install Python dependencies from the workspace pyproject, copy the API and shared backend code, and launch the FastAPI server.
# Dependencies: Docker Compose uses this image definition together with pyproject.toml and the source files under apps/api and services/.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/workspace
ENV PATH=/workspace/.venv/bin:/root/.local/bin:${PATH}
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1

WORKDIR /workspace

RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m ensurepip --upgrade \
    && python -m pip install --no-cache-dir --upgrade pip uv

COPY pyproject.toml README.md ./
RUN uv sync --no-dev --no-install-project

COPY apps/api ./apps/api
COPY services ./services
COPY .env.example ./.env.example

EXPOSE 8000

CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
