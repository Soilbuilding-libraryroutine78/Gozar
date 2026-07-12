# syntax=docker/dockerfile:1

# Multi-stage build for the Gozar Web_Console (React + TypeScript + Vite).
#
# Stages:
#   deps    - install pinned dependencies from the lockfile (cached layer).
#   dev     - Vite dev server with hot reload (used by compose.yml, target: dev).
#   build   - strict type-check + production bundle into /app/dist.
#   runtime - nginx serving the static bundle as a non-root user (prod default).

############################################
# Stage 1: deps
############################################
FROM node:20.19-bookworm-slim AS deps
WORKDIR /app
# Copy only manifests so the dependency layer caches independently of source.
COPY package.json package-lock.json* ./
RUN npm ci

############################################
# Stage 2: dev (hot reload)
############################################
FROM node:20.19-bookworm-slim AS dev
WORKDIR /app
ENV NODE_ENV=development
COPY --from=deps /app/node_modules ./node_modules
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

############################################
# Stage 3: build
############################################
FROM node:20.19-bookworm-slim AS build
WORKDIR /app
ENV NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

############################################
# Stage 4: runtime (production, nginx)
############################################
FROM nginx:1.27-alpine AS runtime
# SPA-aware nginx config (history fallback to index.html).
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# Run nginx as the unprivileged user shipped in the image. The directories nginx
# writes to are made writable for that user.
RUN touch /var/run/nginx.pid \
    && chown -R nginx:nginx /var/run/nginx.pid /var/cache/nginx /usr/share/nginx/html
USER nginx

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --quiet --spider http://127.0.0.1:80/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
