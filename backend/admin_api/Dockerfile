# Build context: backend/  (needs access to ../shared)
FROM python:3.14-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system csense && adduser --system --ingroup csense csense

WORKDIR /app

COPY shared ./shared
COPY admin_api/requirements.txt ./admin_api/requirements.txt
RUN pip install --no-cache-dir -r admin_api/requirements.txt \
    && pip install --no-cache-dir ./shared

COPY admin_api/app ./app

USER csense
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
