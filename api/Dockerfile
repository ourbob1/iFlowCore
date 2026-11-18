# iFlow Core account & sync API
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt && useradd --system --uid 10001 api
COPY iflow_api ./iflow_api
COPY migrations ./migrations
COPY alembic.ini .
USER api
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/healthz').status!=200)"
# Migrations run as a separate step before a deploy (`alembic upgrade head`), not on every start.
CMD ["uvicorn", "iflow_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--no-server-header"]
