FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
# NEXT_PUBLIC_* vars are inlined into the client bundle at `next build` time - a
# docker-compose `environment:` entry (runtime only) has zero effect on them, which is
# why this must be a build ARG threaded into the build stage's own env, not just passed
# at `docker compose up`. Real bug this fixes: production silently fell back to this
# Dockerfile's previous hardcoded default (effectively localhost:8080), which resolves
# to each visitor's own machine, not the server - every Developer Console login failed
# with a generic "Something went wrong" (a network failure, not a real 401).
ARG NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
RUN npm run build

FROM node:20-slim
WORKDIR /app
RUN addgroup --system csense && adduser --system --ingroup csense csense
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER csense
EXPOSE 3000
ENV PORT=3000
CMD ["node", "server.js"]
