#!/usr/bin/env bash
# Hourly mark-status → Percy's GSheet sync (Cloud Run job entrypoint).
#
#   1. GET /api/actions (the live ledger) with the read-only `gcs` grant token
#      + a real User-Agent (CF edge-blocks bot UAs like Python-urllib with 1010).
#   2. `report` → per-user "who still needs to mark & sweep" CSV (mirrors /users;
#      reads the latest snapshot tree/meta from gs://$DATA_BUCKET via ambient SA).
#   3. `sheet-push` → full-replace one named tab in place (values-only clear keeps
#      the header styling + freeze), with an "AUTO" footer disclaimer.
#
# Runs AS the SA (gcs-usage-job@…): ambient ADC covers both the GCS snapshot read
# and the Sheets write (SA is Editor on the sheet) — no key material, no
# impersonation. GCS_USAGE_TOKEN is injected from Secret Manager (see deploy.sh).
# No `set -x`: the token must never reach Cloud Logging.
set -euo pipefail

: "${GCS_USAGE_TOKEN:?GCS_USAGE_TOKEN must be set (Secret Manager)}"
SHEET_ID=${SHEET_ID:?SHEET_ID must be set}
SHEET_TAB=${SHEET_TAB:-Marin GCS — mark status (nag list)}
SITE_URL=${SITE_URL:-https://gcs.oa.dev}
export DATA_BUCKET=${DATA_BUCKET:-oa-gcs-usage-dvx}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "→ fetching $SITE_URL/api/actions" >&2
# Fetch with Python's urllib (the slim base has no curl); the token rides in a
# header (never the URL), and a real User-Agent dodges Cloudflare's 1010 block.
python3 - "$SITE_URL/api/actions" "$work/actions.json" <<'PY'
import os, sys, urllib.request
url, out = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, headers={
    "Authorization": "Bearer " + os.environ["GCS_USAGE_TOKEN"],
    "User-Agent": "gcs-sheet-sync/1.0",
})
with urllib.request.urlopen(req, timeout=60) as r, open(out, "wb") as f:
    f.write(r.read())
PY

gcs-usage report -a "$work/actions.json" -o "$work/mark-status.csv" -s attributed -u "$SITE_URL"

# Static footer prefix; sheet-push appends "; last change <ts>" and only bumps
# that stamp when data actually changed (so no-op hourly runs rewrite nothing).
disc="⟳ Auto-synced hourly from $SITE_URL/users (edits are overwritten — add derived views in a separate tab)"

gcs-usage sheet-push -w "$SHEET_TAB" -D "$disc" "$SHEET_ID" "$work/mark-status.csv"
echo "✓ synced $SHEET_TAB @ $(date -u '+%Y-%m-%d %H:%M UTC')" >&2

# Hourly live-site health check — piggybacks on this already-scheduled job so
# gcs.oa.dev gets checked every hour (catches e.g. the 2026-08-31 missing-D1-
# footer → /users-blank class of outage between daily snapshots). Non-fatal to
# the sync. Posts to #gcs-usage on failure IF a Slack transport is configured
# (SLACK_BOT_TOKEN+SLACK_CHANNEL, else SLACK_WEBHOOK — neither is set on this job
# by default, so it just logs until deploy.sh wires them). NOTE: rebuild the
# sheet-sync image (sheet-sync/build.sh) for this to take effect.
if gcs-usage healthcheck -u "$SITE_URL"; then
  echo "✓ healthcheck OK" >&2
else
  echo "WARN: gcs.oa.dev healthcheck failed" >&2
  python3 - <<'PY' || true
import json, os, urllib.request
msg = "⚠️ gcs.oa.dev health check failed (hourly, from sheet-sync). Debug: `gcs-usage healthcheck`."
bot, chan, hook = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_CHANNEL"), os.environ.get("SLACK_WEBHOOK")
def post(url, payload, headers):
    urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8", **headers}), timeout=30).read()
try:
    if bot and chan:
        post("https://slack.com/api/chat.postMessage", {"channel": chan, "text": msg}, {"Authorization": f"Bearer {bot}"})
    elif hook:
        post(hook, {"text": msg}, {})
    else:
        print("no Slack transport configured — healthcheck failure logged only")
except Exception as e:  # noqa: BLE001 — alerting must never fail the sync
    print(f"slack post failed: {e}")
PY
fi
