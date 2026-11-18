# iFlow Core AI service — CPU image for a small Linux VPS (tested target: IONOS, Ubuntu 24.04, x86_64).
#   docker compose build && docker compose up -d
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false \
    RS_AI_DATA_DIR=/data

# Tesseract for receipt OCR. Add more languages via OCR_LANGS (e.g. "eng spa fra").
ARG OCR_LANGS="eng"
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr $(for l in $OCR_LANGS; do echo "tesseract-ocr-$l"; done) \
 && rm -rf /var/lib/apt/lists/*

ARG TORCH_VERSION=2.14.0
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}"

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY iflow_ai ./iflow_ai
COPY scripts ./scripts

RUN useradd --system --uid 10001 --home /app app \
 && mkdir -p /models /data \
 && chown -R app:app /models /data
USER app

EXPOSE 8080
VOLUME ["/models", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# One worker: models live in process memory; concurrency is handled inside (RS_AI_MAX_CONCURRENCY).
CMD ["uvicorn", "iflow_ai.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
