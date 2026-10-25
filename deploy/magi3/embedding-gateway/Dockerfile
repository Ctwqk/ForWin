FROM python:3.12-slim

ARG PIP_INDEX_URL=""
ARG HF_ENDPOINT="https://hf-mirror.com"
ARG EMBED_MODEL="all-MiniLM-L6-v2"

WORKDIR /app

COPY requirements.txt .
# Linux arm64 torch wheels on PyPI are CPU-only, which is what magi3 needs.
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt

# Bake the model into the image so startup needs no network.
ENV HF_HOME=/opt/hf
RUN HF_ENDPOINT="$HF_ENDPOINT" python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('$EMBED_MODEL', device='cpu')"
ENV HF_HUB_OFFLINE=1

COPY app.py .

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
