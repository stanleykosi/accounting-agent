# Purpose: Build the canonical worker container image for the local demo stack.
# Scope: Install Python dependencies from the workspace pyproject, copy the worker and shared backend code, and launch the worker runtime process.
# Dependencies: Docker Compose uses this image definition together with pyproject.toml and the source files under apps/worker and services/.

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
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        ghostscript \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libglib2.0-0 \
        libharfbuzz0b \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        ocrmypdf \
        shared-mime-info \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN python -m ensurepip --upgrade \
    && python -m pip install --no-cache-dir --upgrade pip uv

RUN groupadd --system appuser \
    && useradd --system --gid appuser --create-home --home-dir /home/appuser appuser

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && /workspace/.venv/bin/python -m celery --version

COPY apps/worker ./apps/worker
COPY services ./services
COPY .env.example ./.env.example

RUN chown -R appuser:appuser /workspace /home/appuser

USER appuser

CMD ["/workspace/.venv/bin/python", "-m", "apps.worker.app.runtime"]
