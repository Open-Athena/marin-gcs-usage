# The serving box (`dt-cloud serve-query`, specs/filter-query-service.md): one
# image for a GCE VM, a Cloud Run service and local / CI runs. Built from the
# repo root (its `.dockerignore` keeps the context small):
#
#   docker build -f deploy/serve-query/Dockerfile -t serve-query .
#   docker run -p 8080:8080 -v $PWD/tmp/fix-root:/index:ro serve-query -A /index   # a local fixture, no auth
#
# On a VM or Cloud Run the argument is the index root (`gs://…`), `-s` a stage
# dir (Cloud Run: an in-memory one, with `-M`), and the bearer token comes from
# `$QUERY_BOX_TOKEN` (a secret, never an argument). `$PORT` is honored.
#
# Installed by uv from the workspace lockfile, `--frozen`, into the image's own
# interpreter, as deploy/sheet-mirror/Dockerfile does: the image runs exactly
# the pins the tests ran.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1 UV_FROZEN=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
RUN uv sync --no-dev --no-editable --package dt-cloud
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["dt-cloud", "serve-query"]
CMD ["/index"]
