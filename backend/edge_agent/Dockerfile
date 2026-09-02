# Build context: backend/edge_agent — self-contained, unlike the other backend
# Dockerfiles. This one deliberately does NOT `COPY shared` or `pip install ./shared`:
# the agent does not depend on csense_shared (see requirements.txt and app/__init__.py
# for why — that package needs sqlalchemy/asyncpg/redis/minio/argon2-cffi/pyjwt, several
# of which want a C toolchain on arm64, for a process whose only job is speaking HTTP).
#
# No apt-get, no gcc, no libpq-dev either. httpx is pure Python and cryptography ships
# prebuilt manylinux/musllinux wheels for aarch64 — this image builds on ARM (Pi/Jetson-
# class edge hardware, this agent's actual target per the plan) with nothing to compile.
FROM python:3.12-slim AS base

RUN addgroup --system csense && adduser --system --ingroup csense csense

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Everything durable this process owns lives under one directory: the sqlite spool, the
# device-local encryption key, and the enrolment credential (see app/config.py's
# CSENSE_STATE_DIR). Created and chowned to the runtime user at build time so that a
# `read_only: true` root filesystem (set at the compose level, not here — this Dockerfile
# doesn't get to decide the deployment's mount layout) still leaves this one path
# writable once it is mounted as a volume: Docker seeds a brand-new named volume from
# whatever already exists at the mount point in the image, ownership included, so the
# volume comes up writable by `csense` without a runtime chown step (which read_only
# would block anyway).
RUN mkdir -p /var/lib/csense-agent && chown csense:csense /var/lib/csense-agent

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER csense

# The only thing this process ever listens on: the local detection-source HTTP endpoint
# a co-located inference process hands events to (see app/source.py — the agent does not
# do inference itself). EXPOSE is documentation, not a publish directive; config.py
# refuses a non-loopback bind without CSENSE_SOURCE_TOKEN, and nothing in this image
# expects the port to be reachable from outside the box it runs on.
EXPOSE 8099

CMD ["python", "-m", "app.main"]
