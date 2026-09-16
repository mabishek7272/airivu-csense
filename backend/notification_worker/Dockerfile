# Build context: backend/  (needs access to ../shared)
FROM python:3.14-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system csense && adduser --system --ingroup csense csense

WORKDIR /app

COPY shared ./shared
COPY notification_worker/requirements.txt ./notification_worker/requirements.txt
RUN pip install --no-cache-dir -r notification_worker/requirements.txt \
    && pip install --no-cache-dir ./shared

COPY notification_worker/app ./app

USER csense

# No port. The worker takes no requests - it polls a queue and calls out. Anything that
# could reach it over the network would be an attack surface with no purpose.
CMD ["python", "-m", "app.main"]
