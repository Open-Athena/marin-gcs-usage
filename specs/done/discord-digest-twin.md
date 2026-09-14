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

## Emoji gotcha (open — needs a permission grant)

Discord silently rewrites `<:name:id>` to bare `:name:` when the *poster* can't use that emoji. Application emoji are usable by the app and by **webhooks the app owns**, and the `#marin-bot-dbg` staging webhook is user-created (`application_id: null`), so the staged OP shows `:arrow_degm30:` as text. Marin Bot has no `MANAGE_WEBHOOKS` (nor `CREATE_GUILD_EXPRESSIONS`), so it can't create its own webhook yet. Fix = grant Marin Bot **Manage Webhooks** on the digest channel(s), then `gcs-usage` creates an app-owned "GCS usage" webhook there (`tmp/discord-webhook-probe.py` is the probe; promote to a `discord-webhook` subcommand once confirmed) and posts through it. The alternative — guild emoji — would put 17 arrows in the server's picker and needs Create Expressions instead.

## Rollout

1. Grant Marin Bot Manage Webhooks on `#marin-bot-dbg` → re-stage through an app-owned webhook, confirm the arrows render.
2. Pick the prod channel (a `#gcs-usage` twin in Marin's server, or `#internal-discuss`), same grant, create the webhook, store its URL + the bot token as GSM secrets (`gcs-usage-discord-digest-webhook`, `marin-discord-bot-token`), mount them as `DISCORD_DIGEST_WEBHOOK` / `DISCORD_BOT_TOKEN` in the Batch job, add `gcs-usage digest -P discord` next to the Slack call in `run.sh`.
3. Backfill the month once (no spacing needed); daily runs append one reply.

[shape-c]: slack-digest-shape-c.md
