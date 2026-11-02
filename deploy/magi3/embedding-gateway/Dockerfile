FROM python:3.12-slim

ARG PIP_INDEX_URL=""
ARG TORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"
ARG HF_ENDPOINT="https://hf-mirror.com"
ARG EMBED_MODEL="all-MiniLM-L6-v2"

WORKDIR /app

COPY requirements.txt .
# PyPI's Linux torch wheels, arm64 included, are CUDA builds that pull in about 4 GB of
# nvidia-* and triton packages. magi3 runs the model on CPU, so torch itself comes from
# PyTorch's CPU index; its dependencies and every other package come from PIP_INDEX_URL.
RUN pip install --no-cache-dir --no-deps --index-url "$TORCH_INDEX_URL" "$(grep '^torch[<>=~!]' requirements.txt)" \
    && pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt \
    && pip check \
    && python -c "import torch; assert torch.version.cuda is None, torch.__version__"

# Bake the model into the image so startup needs no network.
ENV HF_HOME=/opt/hf
RUN HF_ENDPOINT="$HF_ENDPOINT" python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('$EMBED_MODEL', device='cpu')"
ENV HF_HUB_OFFLINE=1

COPY app.py .

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
