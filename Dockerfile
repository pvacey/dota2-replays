# syntax=docker/dockerfile:1

# ---- web: Astro site, built with bun ----------------------------------------
FROM oven/bun:1 AS web
WORKDIR /app/web
COPY web/package.json web/bun.lock ./
RUN bun install --frozen-lockfile
COPY web/ ./
RUN bun run build

# ---- runtime: Python 3.11, uv venv, baked data/ ------------------------------
FROM python:3.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Python deps; cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# data/ (hero/item icons, map, catalog JSONs) baked at build so the container
# needs no network at startup. Slow step (~2.6k CDN downloads), placed above
# the code copies so it only reruns when the lockfile or the script change.
COPY scripts/ scripts/
RUN .venv/bin/python scripts/fetch_assets.py

COPY backend/ backend/
COPY pipeline/ pipeline/
COPY --from=web /app/web/dist/ web/dist/

# Runtime state lives in var/ (parsed matches, job state, replay downloads);
# mount a volume there to keep it.
RUN mkdir -p /app/var \
    && useradd --system --uid 10001 --create-home replays \
    && chown -R replays:replays /app/var

USER replays
ENV PORT=8000
EXPOSE 8000

CMD [".venv/bin/python", "backend/server.py"]
