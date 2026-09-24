# Build context: backend/  (needs access to ../shared)
FROM python:3.12-slim AS base

RUN addgroup --system csense && adduser --system --ingroup csense csense

WORKDIR /app

COPY shared ./shared
COPY demo_api/requirements.txt ./demo_api/requirements.txt
RUN pip install --no-cache-dir -r demo_api/requirements.txt \
    && pip install --no-cache-dir ./shared

COPY demo_api/app ./app

USER csense
EXPOSE 8000

# --no-access-log: this endpoint is public and receives photos of passers-by; the access
# log would record every visitor's IP for no operational benefit.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
