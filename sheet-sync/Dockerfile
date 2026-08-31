# Standalone lightweight image for the hourly mark-status → GSheet sync.
# Independent of the heavy daily-snapshot image (job/, root Dockerfile): this
# just needs the `gcs-usage` CLI (`report` + `sheet-push`) — no site build, no
# wrangler, no disk-tree engine (imported lazily only by access/reactive).
# Build context is the repo root (see cloudbuild.yaml) so it can copy marin/.
FROM python:3.12-slim
WORKDIR /app
COPY marin/pyproject.toml ./marin/
COPY marin/src ./marin/src
RUN pip install --no-cache-dir "./marin[sheets]"
COPY sheet-sync/sync.sh ./sync.sh
ENTRYPOINT ["bash", "sync.sh"]
