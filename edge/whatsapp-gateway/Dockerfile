# WhatsApp Gateway — vendored from Evolution Go, built headless.
#
# Differences from the upstream Dockerfile:
#   - `manager/dist` (their browser console) is not copied, because it is not vendored.
#     CSense drives this service through its API and provides its own instance-management
#     UI in the Developer Console. See README.md for why that is a licence decision as
#     well as an architectural one.
#   - Runs as a non-root user (TRD-SEC-008). Upstream runs as root.
#   - TZ is UTC. All timestamps in this platform are UTC and rendered in the site's own
#     timezone at display time; a container-local timezone would put a second, silent
#     conversion in the middle of that.

FROM golang:1.25.0-alpine AS build

RUN apk update && apk add --no-cache git build-base libjpeg-turbo-dev libwebp-dev

WORKDIR /build

# Dependency files first, so the module download layer caches across source changes.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

ARG VERSION=dev
RUN CGO_ENABLED=1 go build -ldflags "-X main.version=${VERSION}" -o server ./cmd/evolution-go

FROM alpine:3.24.2 AS final

# ffmpeg and libwebp: media transcoding for image sends.
# poppler-utils: pdftoppm, used to rasterize page 1 of a PDF for document thumbnails.
RUN apk update && apk add --no-cache tzdata ffmpeg libjpeg-turbo libwebp poppler-utils \
    && addgroup -S gateway && adduser -S -G gateway gateway

WORKDIR /app

COPY --from=build /build/server .
COPY --from=build /build/VERSION ./VERSION
# Kept in the image because the licence requires it (Apache 2.0 §4(d)).
COPY --from=build /build/NOTICE ./NOTICE
COPY --from=build /build/LICENSE ./LICENSE

# Create the log directory owned by the runtime user. A Docker named volume inherits
# ownership from the image path it covers, so without this the mount lands root-owned and
# the non-root process cannot write - losing exactly the log you need when sends start
# failing.
RUN mkdir -p /app/logs && chown -R gateway:gateway /app

ENV TZ=UTC

USER gateway
EXPOSE 8080

ENTRYPOINT ["/app/server"]
