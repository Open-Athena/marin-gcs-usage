# Cloud Run job image for daily storage snapshots (see job/run.sh).
# Stage 1: build the static site (data dirs are overlaid at runtime).
FROM node:22-slim AS site
WORKDIR /site
COPY site/package.json site/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY site/ ./
RUN pnpm build

# Stage 2: pipeline + wrangler (node for wrangler; python for gcs-usage).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/* \
    && npm install -g wrangler
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=site /site/dist ./dist
COPY job ./job
ENTRYPOINT ["bash", "job/run.sh"]
