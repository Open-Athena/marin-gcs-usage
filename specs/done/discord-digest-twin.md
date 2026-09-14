# Discord twin of the monthly `#gcs-usage` digest thread

`gcs-usage digest -P discord` converges the same Shape-C monthly thread that the Slack digest posts ([`slack-digest-shape-c.md`][shape-c]) into a Discord channel: one thread per month, an OP edited in place daily (month-to-date headline, per-week bullets, the mosaic plot) and one reply per scan under a per-scan sender (headline name + colour-coded trend-arrow avatar). Shipped 2026-09-14; staged in `#marin-bot-dbg`.

## Shape on Discord's split transports

Discord has no single identity that can both carry a per-message sender and own a thread, so the twin uses thrds's two clients (pin ≥ `7054898`, which added webhook file attachments + `allowed_mentions`):

| piece | Slack (`post_digest`) | Discord (`converge_discord`) |
| --- | --- | --- |
| OP | bot `chat.postMessage` with `username` = "GCS usage — Month YYYY", `icon_emoji: :calendar:` | **webhook** post, `username` = same title, `avatar_url` = `gcs-usage-icons.pages.dev/calendar.png` (Twemoji 📅; webhooks take image URLs only) |
| plot | rendered PNG hosted on the icons Pages project, `![…](url)` in the body → Slack image block; needs the wrangler deploy + CDN-propagation wait | rendered PNG **attached** (`files=[png]`), re-uploaded on every OP edit; no hosting, no deploy, no wait |
| thread | replies address the OP `ts` | **bot** `create_thread(op_id, title)` off the webhook OP (a webhook can't open threads; `thread_id == op_id`) |
| replies | bot post, `username` = headline, `icon_url` = `av_deg±N.png` | webhook post into the thread, same `username`/`icon_url` |
| spacing | ≥ 300 s between replies or Slack collapses the later senders' chrome (backfill `-D 305`) | none: Discord groups by *displayed* sender and every headline differs |
| link previews | n/a | `suppress_embeds` on every post (the OP's dashboard link would otherwise embed the site's og card) |
| mentions | n/a | `allowed_mentions=NO_MENTIONS` (paths/names can never ping) |
| `:arrow_degN:` | Slack custom emoji | **application emoji** on the bot, rendered `<:name:id>`; negatives are `arrow_degmN` (Discord names allow no `-`) |
| state | `digest/<YYYY-MM>.json` | `digest/discord/<webhook_id>/<YYYY-MM>.json` (`op_id`, `thread_id`, `posted{date: id}`) — keyed by webhook because only the posting webhook can edit its messages, and a staging webhook must never masquerade as prod |

Every content function (`deg`, `op_body`, `reply`, `rows_from_meta`, `render_plot`) is shared verbatim; the body markdown (bold, italics, masked links) renders identically on both. `op_body(plot_url=None)` drops the image line for the attachment path; `discordify()` rewrites the emoji shortcodes. `converge_discord` takes the clients + emoji map as arguments and is unit-tested against fakes (fresh month → OP + thread + reply; next day → OP edit + one reply; same day → OP edit only).

## Pieces

- `gcs_usage/discord_api.py` — the three REST calls thrds doesn't wrap: `webhook_info` (which channel/id a webhook URL is), `app_emojis`, `upload_app_emoji`. Bot calls send the `DiscordBot (url, version)` User-Agent Discord's Cloudflare front requires.
- `gcs-usage discord-emoji [-n]` — idempotent upload of `job/icons/arrows/arrow_deg*.png` as application emoji; prints `name id`. Run once per bot; the digest resolves ids at runtime.
- `gcs-usage digest -P discord [-w URL] [-b TOKEN] [-m YYYY-MM]` — `DISCORD_DIGEST_WEBHOOK` + `DISCORD_BOT_TOKEN` by default. `-n` still prints the platform-neutral body.
- `job/icons/calendar.png` (Twemoji `1f4c5`, CC-BY 4.0) deployed with the icons project.

## Emoji gotcha (resolved: the webhook must be app-owned)

Discord silently rewrites `<:name:id>` to bare `:name:` when the *poster* can't use that emoji. Application emoji are usable by the app and by **webhooks the app owns** (`application_id` = the bot's app); a user-created webhook (channel settings → Integrations → New Webhook, `application_id: null`) strips them — the first `#marin-bot-dbg` staging OP showed `:arrow_degm30:` as text. So the digest's webhook is created *by the bot*: `gcs-usage discord-webhook -c '#gcs-usage' -g <guild>` (reuses an existing app-owned "GCS usage" webhook by name; needs the bot's role to have **Manage Webhooks** on the channel; prints the URL on stdout — redirect it, it embeds a secret). Verified 2026-09-14 in `#gcs-usage`: the readback OP carries `<:arrow_degm30:id>`. Guild emoji would have worked from any webhook but need Create Expressions and land in the server's picker.

## Rollout

1. ~~Grant Marin Bot Manage Webhooks, re-stage through an app-owned webhook, confirm the arrows render.~~ Done 2026-09-14: `#gcs-usage` (private: Ryan + the Marin Archiver role, i.e. Marin Bot) holds the September thread through app-owned webhook `1549163390683971686`; Marin Dev gets added once the format settles. The earlier `#marin-bot-dbg` thread is orphaned (its webhook is user-created).
2. Store the webhook URL + the bot token as GSM secrets (`gcs-usage-discord-webhook`, `marin-discord-bot-token`), mount them as `DISCORD_DIGEST_WEBHOOK` / `DISCORD_BOT_TOKEN` in the Batch job, add `gcs-usage digest -P discord` next to the Slack call in `run.sh`. The weekly report can post through the same webhook (`-w`), which retires the `#internal-discuss` secret grant.
3. Daily runs append one reply; no backfill spacing needed.

[shape-c]: slack-digest-shape-c.md
