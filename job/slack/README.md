# `#cw-s3-usage` Slack app

`cw-usage-bot.manifest.json` is the manifest for the **CoreWeave Usage Bot** app (see `specs/cw-slack-digest.md`). It is a separate app from GCS Usage Bot: per-message sender overrides don't hide the app name in Slack's Unread/Threads views, so the app itself must say CoreWeave.

Create at api.slack.com/apps → *Create New App* → *From a manifest* → paste the JSON; install to the workspace; the Bot User OAuth Token (`xoxb-…`) goes to Secret Manager as `cw-s3-slack-bot-token` (accessor grant to `gcs-usage-job@`). The manifest carries no comments because Slack rejects unknown top-level keys.

Scopes = what thrds's `SlackClient` calls (`chat.postMessage/update/delete/getPermalink`, `conversations.history/replies/info/list`, `users.info`, `emoji.list`) + `chat:write.customize` for the per-message sender/avatar; `chat:write.public` lets it post to public channels without an `/invite`.
