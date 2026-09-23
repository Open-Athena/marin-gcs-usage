# Cloud Run job image for daily storage snapshots (see job/run.sh).
# Stage 1: build the static site (data dirs are overlaid at runtime).
# The site is a pnpm-workspace member (with in-tree @disk-tree/react), so the
# build context is the repo root: copy the workspace manifests + the members
# the site needs (ui/ contributes only its package.json; its deps install
# too — the workspace lockfile is one unit — but stay in this cached layer).
FROM node:22-slim AS site
WORKDIR /repo
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml ./
COPY ui ./ui
COPY packages/treemap ./packages/treemap
COPY packages/react ./packages/react
COPY site ./site
RUN corepack enable && pnpm install --frozen-lockfile
RUN cd site && pnpm build
# disk-tree's wheel force-includes its built UI (ui/dist), so build it here too
RUN cd ui && pnpm build

# Stage 2: pipeline + wrangler (node for wrangler; python for dt-cloud).
# Node comes from the node:22-slim stage (same Debian base) — Debian's apt
# nodejs is v20, below wrangler's floor (≥22 as of wrangler 4.116).
FROM python:3.12-slim
COPY --from=site /usr/local/bin/node /usr/local/bin/node
COPY --from=site /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g wrangler@4
# GNU time: the gate mode (`job/run.sh` GATE=1) reports each import's peak RSS.
# curl: thrds's Discord clients shell out to it (the 2026-09-15 job's Discord
# digest died with `FileNotFoundError: 'curl'`); python:slim ships none.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends time curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# disk-tree engine (root project): fan-out listing tasks run `disk-tree
# bulk-list`; its wheel force-includes ui/dist (built in stage 1).
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=site /repo/ui/dist ./ui/dist
RUN pip install --no-cache-dir ".[gcs,s3]"
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
# [plot]: matplotlib for the digest's OP mosaic (dt_cloud.digest_plot)
RUN pip install --no-cache-dir "./cloud[plot]"
COPY --from=site /repo/site/dist ./dist
COPY job ./job
# One image, two scheduled jobs: the GCS fleet job (`job/run.sh`, tag `latest`)
# and the CoreWeave scan job (`job/cw-run.sh`, tag `cw`). `job/build.sh` picks
# the script by tag (`--build-arg JOB=cw-run.sh`); the default is the GCS job.
ARG JOB=run.sh
ENV JOB_SCRIPT=$JOB
ENTRYPOINT ["bash", "-c", "exec bash job/$JOB_SCRIPT \"$@\"", "--"]
