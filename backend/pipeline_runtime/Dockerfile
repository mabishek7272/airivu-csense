# Build context: backend/  (needs access to ../shared)
FROM python:3.14-slim AS base

# Unlike the edge agent, this service runs on the full x86 production box (CLAUDE.md: "16
# cores / 32GB RAM, no GPU") and is not fighting an ARM/no-compiler constraint - it
# legitimately needs OpenCV's real FFmpeg/RTSP decode path, not a stub.
#   - libgl1/libglib2.0-0: what `cv2` itself needs to import at all (same packages
#     `ai_runtime/Dockerfile` already installs for the same reason).
#   - ffmpeg: provides the real libavformat/libavcodec/libswscale shared libraries
#     `opencv-python-headless`'s bundled FFmpeg videoio backend dynamically loads to open
#     an `rtsp://` URL - `cv2.imdecode` (ai-runtime's own use of `cv2`) never touches this
#     path, but `grab_frame`'s `cv2.VideoCapture(..., cv2.CAP_FFMPEG, ...)` does.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev libglib2.0-0 libgl1 ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system csense && adduser --system --ingroup csense csense

WORKDIR /app

COPY shared ./shared
COPY pipeline_runtime/requirements.txt ./pipeline_runtime/requirements.txt
RUN pip install --no-cache-dir -r pipeline_runtime/requirements.txt \
    && pip install --no-cache-dir ./shared

COPY pipeline_runtime/app ./app

USER csense

# No port. This service takes no requests - it discovers active pipeline assignments and
# pulls camera frames on its own schedule. Anything that could reach it over the network
# would be an attack surface with no purpose (same reasoning as notification-worker).
CMD ["python", "-m", "app.main"]
