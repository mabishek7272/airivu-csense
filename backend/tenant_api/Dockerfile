# Build context: backend/  (needs access to ../shared)
FROM python:3.14-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system csense && adduser --system --ingroup csense csense

WORKDIR /app

COPY shared ./shared
COPY tenant_api/requirements.txt ./tenant_api/requirements.txt
RUN pip install --no-cache-dir -r tenant_api/requirements.txt \
    && pip install --no-cache-dir ./shared

COPY tenant_api/app ./app

# TRD-SEC-008: non-root, read-only filesystem (compose mounts tmpfs at /tmp),
# no extra Linux capabilities, drop root privileges before serving.
USER csense
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
