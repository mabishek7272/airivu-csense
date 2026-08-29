# Build context: backend/  (needs access to ../shared)
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system csense && adduser --system --ingroup csense csense

WORKDIR /app

COPY shared ./shared
COPY ai_runtime/requirements.txt ./ai_runtime/requirements.txt

# CPU-only torch: the default wheel pulls ~2.5 GB of CUDA libraries this deployment
# cannot use. Installed first so the heavy layer caches independently of app code.
#
# insightface with --no-deps: its own declared dependency is `opencv-python` (the GUI
# build), which shares an import path with `opencv-python-headless` (used everywhere else
# in this image) and cannot coexist with it - confirmed empirically, not assumed (installing
# both then removing either leaves `cv2` unimportable). Its real dependencies other than
# opencv are listed in requirements.txt instead.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        torch torchvision \
    && pip install --no-cache-dir --no-deps insightface==1.0.1 \
    && pip install --no-cache-dir -r ai_runtime/requirements.txt \
    && pip install --no-cache-dir ./shared

COPY ai_runtime/app ./app

# Artifact cache is a volume; the model pool needs it writable, so it is created and
# chowned here rather than relying on the read-only rootfs.
RUN mkdir -p /var/cache/csense/models && chown -R csense:csense /var/cache/csense

USER csense
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
