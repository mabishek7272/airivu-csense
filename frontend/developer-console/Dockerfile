FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
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
