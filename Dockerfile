FROM node:22-slim AS world-studio-builder

WORKDIR /app/frontend/world-studio

COPY frontend/world-studio/package.json frontend/world-studio/package-lock.json ./
RUN npm ci
COPY frontend/world-studio/index.html frontend/world-studio/tsconfig.json frontend/world-studio/vite.config.ts ./
COPY frontend/world-studio/src/ src/
RUN npm run build

FROM python:3.13-slim AS python-base

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.10.3 /uv /usr/local/bin/uv
ARG FORWIN_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$FORWIN_SOURCE_REVISION
ENV FORWIN_SOURCE_REVISION=$FORWIN_SOURCE_REVISION
ENV PATH="/app/.venv/bin:$PATH"
ENV UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
COPY alembic.ini .
COPY forwin/ forwin/
COPY forwin_skills/ forwin_skills/

RUN uv sync --frozen --no-dev --extra runtime --no-editable
COPY --from=world-studio-builder /app/frontend/world-studio/dist/ frontend/world-studio/dist/
COPY browser_extension/ browser_extension/
COPY scripts/ scripts/

RUN mkdir -p /app/data

ENV MINIMAX_API_KEY=""
ENV MINIMAX_BASE_URL="https://api.minimaxi.com/v1"
ENV MINIMAX_MODEL="MiniMax-M2.7"
ENV FORWIN_DATABASE_URL="postgresql+psycopg://forwin:forwin@postgres:5432/forwin"

FROM python-base AS publisher-browser-runtime

# Upload commands can occupy the extension beyond the default 90-second heartbeat window.
ENV FORWIN_PUBLISHER_HEARTBEAT_STALE_SECONDS=300

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium xvfb xauth ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/*
RUN python -m playwright install --with-deps chromium

CMD ["bash", "-lc", "exec scripts/launch_linux_extension_browser.sh"]

FROM python-base AS forwin-runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8899/health')" || exit 1

CMD ["uvicorn", "forwin.api:app", "--host", "0.0.0.0", "--port", "8899"]
