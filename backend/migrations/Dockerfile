# Build context: backend/  (needs access to ../shared)
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY shared ./shared
COPY migrations/requirements.txt ./migrations/requirements.txt
RUN pip install --no-cache-dir -r migrations/requirements.txt \
    && pip install --no-cache-dir ./shared

COPY migrations ./migrations
RUN chmod +x /app/migrations/entrypoint.sh

WORKDIR /app/migrations
CMD ["/app/migrations/entrypoint.sh"]
