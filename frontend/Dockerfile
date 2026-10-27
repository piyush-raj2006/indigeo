# IndiGeo web image: Next.js 16 standalone output.
#
# Build with the API URL baked in, because NEXT_PUBLIC_* values are inlined at build time:
#   docker build -f frontend/Dockerfile \
#     --build-arg NEXT_PUBLIC_API_BASE=https://api.example.org/api/v1 \
#     -t indigeo-web ./frontend

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_BASE
# A production bundle must not point at loopback: the value is inlined at build time and a
# remote browser would call its own machine. Compose passes it from .env/.env.production.
RUN case "$NEXT_PUBLIC_API_BASE" in \
      ""|*//localhost*|*//127.0.0.1*) \
        echo "error: NEXT_PUBLIC_API_BASE must be the public API origin (set it as a build arg; loopback is rejected)" >&2; \
        exit 1 ;; \
    esac
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -g 10001 -S nodejs && adduser -u 10001 -S nextjs -G nodejs
COPY --from=build /app/public ./public
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
