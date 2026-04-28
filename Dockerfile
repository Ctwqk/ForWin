FROM node:22-slim AS world-studio-builder

WORKDIR /app/frontend/world-studio

COPY frontend/world-studio/package.json frontend/world-studio/package-lock.json ./
RUN npm ci
COPY frontend/world-studio/index.html frontend/world-studio/tsconfig.json frontend/world-studio/vite.config.ts ./
COPY frontend/world-studio/src/ src/
RUN npm run build

FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
COPY alembic.ini .
COPY forwin/ forwin/

RUN pip install --no-cache-dir .
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium xvfb xauth ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/*
RUN python -m playwright install --with-deps chromium
COPY --from=world-studio-builder /app/frontend/world-studio/dist/ frontend/world-studio/dist/
COPY browser_extension/ browser_extension/
COPY scripts/ scripts/

RUN mkdir -p /app/data

EXPOSE 8899

ENV MINIMAX_API_KEY=""
ENV MINIMAX_BASE_URL="https://api.minimaxi.com/v1"
ENV MINIMAX_MODEL="MiniMax-M2.7"
ENV FORWIN_DATABASE_URL="postgresql+psycopg://forwin:forwin@postgres:5432/forwin"

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8899/health')" || exit 1

CMD ["uvicorn", "forwin.api:app", "--host", "0.0.0.0", "--port", "8899"]
