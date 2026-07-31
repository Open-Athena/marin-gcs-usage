# Cloud Run job image for daily storage snapshots (see job/run.sh).
# Stage 1: build the static site (data dirs are overlaid at runtime).
FROM node:22-slim AS site
WORKDIR /site
COPY site/package.json site/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY site/ ./
RUN pnpm build

# Stage 2: pipeline + wrangler (node for wrangler; python for gcs-usage).
# Node comes from the node:22-slim stage (same Debian base) — Debian's apt
# nodejs is v20, below wrangler's floor (≥22 as of wrangler 4.116).
FROM python:3.12-slim
COPY --from=site /usr/local/bin/node /usr/local/bin/node
COPY --from=site /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g wrangler@4
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=site /site/dist ./dist
COPY job ./job
ENTRYPOINT ["bash", "job/run.sh"]
