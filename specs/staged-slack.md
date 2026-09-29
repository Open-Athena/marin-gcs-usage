# Staged-deletion review in Slack

Staged deletions (the table's trash gesture, `/staged`) announce themselves in an admin Slack channel, where admins can review and act without opening www. Ryan, 2026-09-29: cw → `#cw-s3-admin` (`C0C53CPM886`), gcs → `#gcs-admin` (`C0C579HNRFB`); "try both [notify + act from Slack], I'm curious whether acting from Slack can be made good enough / preferable".

## Shape

- **One thread per staged plan.** The parent message is re-rendered on every event: item and batch counts, who staged, the latest dry-run's result and whether it still matches the plan, the last real run. Every event is a reply: a stage batch (who, count, memo, the first prefixes), an unstage, a batch rejected, a run dispatched (www or Slack), a run finished (totals; for a real run, the undo deadline).
- **Buttons.** Parent: *Open in www*, *Dry-run*, *Delete for real…*. Each batch reply: *Reject batch*, *View in www*.
- **Authority is the site's.** `/slack/actions` verifies Slack's signature (the app's signing secret), maps the clicker to their email (`users.info`, `users:read.email`), and applies the www rules: dispatch = admin (staff domain or `admin_emails`); reject = admin or the batch's stager.
- **Real deletion from Slack is gated** (`realGate`): only after a *finished* dry-run of exactly the current item set (`deletion_runs.plan_digest` = sha-256 of the sorted prefixes), with no run in flight; it runs against that dry-run's scan; the button carries the item count and the dry-run's bytes/objects in Slack's confirm dialog; a button drawn for an older item set is refused. The button only appears once the gate is open.
- **Results arrive on their own.** The Batch job's exit trap calls `/api/plan-sweep/jobs` with the job's read grant (`cw-s3-job-grant`); the reflector now reflects a run as soon as its summary exists (not only at Batch terminal state) and posts it to the thread.
- **Best-effort.** Notifications run after the response (`waitUntil`); a Slack failure never fails a gesture. No Slack config (`SLACK_BOT_TOKEN` + `SLACK_ADMIN_CHANNEL`) = no posts.

## Pieces

`_lib/slack.ts` (Web API, signature, digest) · `_lib/stagedSlack.ts` (gate, rendering, notifier) · `_lib/planDispatch.ts` + `_lib/runReflect.ts` (moved out of the plan-sweep routes, shared) · `functions/slack/actions.ts` · `migrations/cw/0005_staged_slack.sql` (`plans.slack_channel/slack_ts`, `deletion_runs.plan_digest`) · vars `SLACK_ADMIN_CHANNEL`, `EXECUTOR` · secrets `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` · Slack app manifest (`job/slack/cw-usage-bot.manifest.json`): `users:read.email`, interactivity URL.

## gcs

The notifier is store-generic (it reads plans, batches, runs). Dispatch buttons need `EXECUTOR = "plan-sweep"`; gcs's executor is the `sweep` route family, so on gcs the thread shows *Open* and *Reject* until a gcs dispatch path is wired (`planDispatch` equivalent + `plan_digest` recorded by its dispatch). gcs's lineage needs the same migration, and its own app's token/secret.

## Open

- Does acting from Slack beat www? Watch: whether admins use the buttons, whether the dry-run numbers in the thread are enough context to confirm a real deletion.
- A Batch job that dies before writing a summary is reflected (and announced) only when something next reads `/api/plan-sweep/jobs` — `/staged` or any Slack click.
