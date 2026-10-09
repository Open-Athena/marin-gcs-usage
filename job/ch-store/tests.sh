#!/usr/bin/env bash
# Run small fixtures beside the remote ClickHouse server, in an ephemeral image.
# Do not overlap separate invocations: ingest's server-side staging is shared.
set -euo pipefail
TEST_IMAGE=$(cat /data/image)
exec docker run --rm --network host -v /data:/data \
  -e PYTHONPATH=/data/src -e CLICKHOUSE_URL=http://localhost:8123 \
  -e HL1_NATIVE_BINARY="${HL1_NATIVE_BINARY:-}" \
  -e HF_NATIVE_BINARY="${HF_NATIVE_BINARY:-}" \
  -e HL2_NATIVE_BINARY="${HL2_NATIVE_BINARY:-}" \
  -e HL2_NATIVE_SOURCE_BINARY="${HL2_NATIVE_SOURCE_BINARY:-}" \
  --entrypoint bash "$TEST_IMAGE" -c '
    set -euo pipefail
    uv pip install --system pytest
    cd /data/test-checkout
    exec python3 -m pytest "$@"
  ' pytest "$@"
