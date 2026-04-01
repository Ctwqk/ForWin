FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
COPY forwin/ forwin/
COPY browser_extension/ browser_extension/

RUN pip install --no-cache-dir .
RUN python -m playwright install --with-deps chromium

RUN mkdir -p /app/data

EXPOSE 8899

ENV MINIMAX_API_KEY=""
ENV MINIMAX_BASE_URL="https://api.minimaxi.com/v1"
ENV MINIMAX_MODEL="MiniMax-M2.7"
ENV FORWIN_DB_PATH="/app/data/novel.db"

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8899/health')" || exit 1

CMD ["uvicorn", "forwin.api:app", "--host", "0.0.0.0", "--port", "8899"]
