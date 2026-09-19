FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
# Vite inlines VITE_* vars into the client bundle at `npm run build` time, same as
# Next.js's NEXT_PUBLIC_* (see frontend/developer-console/Dockerfile's own comment on
# the exact bug that pattern causes if these are only set under compose's `environment:`
# instead of threaded through as real build ARGs).
ARG VITE_LIVE_DEMO_EMAIL
ARG VITE_LIVE_DEMO_PASSWORD
ENV VITE_LIVE_DEMO_EMAIL=$VITE_LIVE_DEMO_EMAIL
ENV VITE_LIVE_DEMO_PASSWORD=$VITE_LIVE_DEMO_PASSWORD
RUN npm run build

# nginx-unprivileged runs as a non-root user out of the box (TRD-SEC-008) and serves the
# static build directly — no Node runtime, npm cache, or writable HOME needed in the
# final image. This site has no client-side routes and calls no backend at runtime, so
# the image is exactly this: a build stage and a static file server.
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
