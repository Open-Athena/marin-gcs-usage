# Session log: optional, time-boxed user-action logging

When someone reports a bug, an admin opens their session's event log and sees what they did, what the site fetched, and what failed. Logging is off by default and turns itself off on a date, so it can be switched on around an announcement (the Discord post) and can't be left on by accident.

This is an event log, not a screen recorder: no DOM snapshots, no form contents other than the path filter.

## Switch: `SESSION_LOG_UNTIL`

A plain `[vars]` value; unset = off. No deployment carries a default.

- `SESSION_LOG_UNTIL = "2026-10-17"`: on through the end of that UTC day (off from `2026-10-18T00:00Z`). A full ISO instant (`2026-10-17T18:00Z`) ends at that instant.
- A value more than 31 days ahead is refused (logging stays off and the config endpoint says why), so a typo like `2027-10-17` can't turn it on for a year.
- Expiry is checked on both sides: the server stops accepting batches, and an open tab stops logging at the instant (it reads `until` at boot).
- `SESSION_LOG_RETAIN_DAYS` (default 30): retention, see below.

Leave the var set after the window closes: it is inert past its date but keeps the retention purge running (see Retention).

## What is recorded

Each batch carries a per-tab session id (`sid`, in `sessionStorage`, so it survives reloads of that tab), a per-page-load id (`pid`), a sequence number, and the build SHA (`VITE_BUILD_SHA`, `git rev-parse --short=10 HEAD` at build time). The server adds the signed-in identity (email and `via`, from the request's session, never from the client) and the user agent. Each event carries a client timestamp (`t`, epoch ms) and a kind (`k`):

| kind | fields | source |
|---|---|---|
| `start` | `u` (URL), `ref` (referrer origin) | page load |
| `nav` | `u` | `history.pushState` / `replaceState` / `popstate`, when the URL changed (route, `?d=`, `f=`, drills, `?o=`, …) |
| `click` | `n` (`data-log` name), `l` (label: `aria-label`, else text, ≤ 60 chars), `x` (checkbox state) | capture-phase click on an interactive control (button, link, menu item, option, tab, checkbox / radio, `[data-log]`) |
| `filter` | `n`, `q` | `input` on a `[data-log-input]` field, debounced 800 ms. Only the path filter carries it |
| `pick` | `n`, `v` | `change` on a `<select>`; `slog('pick')` from custom pickers (assign, scan) |
| `prefetch` | `w` (what), `p` (path) | hover-intent prefetches (`prefetchCover`) |
| `api` | `m` (method, if not GET), `u`, `s` (status, 0 = network), `ms`, `c` (`x-cache`), `qe` (`x-query-engine`), `err` (the JSON body's `error`, for non-2xx), `b` (request-body summary) | wrapped `window.fetch` |
| `error` | `msg`, `src` (file:line:col), `st` (stack) | `window` `error` |
| `reject` | `msg`, `st` | `unhandledrejection` |
| `console` | `msg` | `console.error` |
| `viewport` | `w`, `h`, `dpr` | boot and `resize` (debounced 500 ms) |
| `vis` | `s` (`visible` / `hidden`) | `visibilitychange` |
| `dropped` | `n` | buffer overflow (oldest events dropped) |

### What is not recorded

- Form contents other than the path filter (memos, names, emails, dates, the admin console's fields): only `[data-log-input]` fields report `input`.
- Secrets: URL params `key`, `token`, `code`, `state`, `nonce`, `access_token`, `id_token`, `sig`, `signature`, `secret`, `password` are replaced by `…` in every recorded URL (`start`, `nav`, `api`). Cross-origin request URLs are recorded as origin + path, no query. Headers are never recorded.
- Request bodies: an assignment / staging POST records only a summary — per top-level key, an array's length, a `kind`/`op`/`action`/`mode` string, else the value's type (`{items: 12, kind: "assign", memo: "string"}`).
- No DOM snapshots, no keystrokes, no mouse movement.

Strings are truncated at log time (labels 60, filter 200, URLs 300, messages 300, stacks 1500).

## Transport

- Client-side buffer; flush every 10 s, when 200 events accumulate, on `visibilitychange → hidden`, and on `pagehide`. Interval flushes use `fetch(…, {keepalive: true})` (through the original, unwrapped `fetch`: the logger never logs itself); hide/pagehide use `navigator.sendBeacon`. Nothing awaits a flush and failures are dropped, never retried: the log can lose a batch, it can't slow the app.
- Caps: a batch's body ≤ 60 000 bytes (split across several posts otherwise; `sendBeacon` / `keepalive` share a 64 KiB in-flight budget), buffer ≤ 1000 events (oldest dropped, counted in a `dropped` event), ≤ 20 000 events per page load.
- Server: `POST /api/session-log` (`requireViewer`; anonymous `public` viewers are logged with no email on a `PUBLIC_READ` deploy). Body ≤ 64 KiB (413), ≤ 300 events (413), schema-validated (400), per session ≤ 20 000 events (429). Replies 204. A response of 503 (off / expired) stops the client.
- Boot: `GET /api/session-log` (`no-store`) answers `{enabled, until}`; `enabled` only while the window is open *and* the request has a viewer identity, so the sign-in page never logs. While off it costs that one request per page load; the logger itself is a lazily imported chunk, loaded only when on. Errors raised before the chunk arrives are held (≤ 20) and replayed into the log.

## Storage: D1

Compared:

| | (a) D1 table | (b) R2 object per batch | (c) Workers Analytics Engine |
|---|---|---|---|
| infra | none new: the `DB` binding exists on every app deployment; one additive migration | a bucket binding (gcs has `INDEX_R2`; others none) | a new dataset binding, plus a CF API token secret: AE is queried only over the SQL HTTP API, not from the binding |
| cost (a few days, ~100 users) | ~2 rows written per flush (batch row + session upsert, + index entries); a busy tab ≈ 1–2K rows/h — well inside the paid plan's 50M rows/month | Class A op per batch (~$4.50/M); listing for the session index is Class A too | free tier 100K points/day, then $0.25/M |
| write limits | ~1K writes/s per DB; fine at this volume | none in practice | 25 points per invocation (a batch of 200 events doesn't fit as one point each), 16 KB blobs per point |
| query ease | SQL: per-user, per-session, error counts in one indexed query | list prefix + GET + parse every object; the session list needs a separate index | SQL, but sampled under load (bad for "show me *this* session exactly"), and only via the API token |
| retention | a `DELETE … WHERE received_ts < ?` (indexed) | lifecycle rule (cheap, but infra outside the repo) | fixed 3 months, not configurable |

**Choice: D1.** No new infrastructure (the endpoint already lives next to the `DB` binding and the `requireAdmin` gate), exact (unsampled) per-session reads, and the session list is a plain indexed query. Volume is a short window of human clicks, far below D1's limits. One row per *batch* (events as a JSON array) rather than per event keeps rows written ~100× lower and the timeline a single `SELECT`.

### Schema (`migrations/cw/0018_session_log.sql`; gcs: the same file as `migrations/gcs/0042_session_log.sql`)

Additive only: two new tables, no foreign keys (so the purge can delete in any order).

- `session_log` — one row per `sid`: `email`, `via`, `build`, `ua`, `started_ms`, `last_ms` (client clock), `n_events`, `n_errors` (`error` + `reject` + `console` + `api` with `s` 0 or ≥ 500), `n_batches`, `first_url`, `received_ts` (epoch s, server clock: what retention keys on). Indexes on `received_ts`, `(email, last_ms)`.
- `session_log_batches` — PK `(sid, pid, seq)`; `received_ts`, `first_ms`, `last_ms`, `n`, `events` (JSON array as validated). A duplicate `(sid, pid, seq)` (a retried beacon) is ignored and counts nothing (`INSERT OR IGNORE`, the session row bumped only when a row was inserted).

### Retention

`SESSION_LOG_RETAIN_DAYS` (default 30). The purge (`DELETE` from both tables where `received_ts` is older than the cutoff) runs from the endpoint, throttled to once per hour per isolate: on any `GET /api/session-log` (every page load hits it) and on each new page-load's first batch. It runs whenever `SESSION_LOG_UNTIL` is set, expired or not, so the data ages out after the window closes as long as the site gets traffic. A missing table (migration not applied) makes the purge a nop. To end it early: `DELETE FROM session_log_batches; DELETE FROM session_log;`.

## Viewer: `/admin/sessions`

Admin-only (`requireAdmin` on the APIs; the page links from `/admin`).

- `GET /api/session-log/sessions?email=&since=&errors=1&limit=` — newest first (`last_ms`), ≤ 500.
- `GET /api/session-log/sessions/<sid>` — the session row plus its events, flattened across batches, ordered by `t` (ties by `pid`, `seq`, position).

The list: user, start, duration, events, errors (highlighted), build; filters for user (substring), since (date) and errors-only, kept in the URL. The session view: a DIY SVG strip of the session's events on a time axis (ticks by kind, errors red, hover = floating-ui tooltip, click = jump to the row), then a timeline table (offset, kind, summary), errors highlighted, each row with "open this view" — the URL in effect at that moment (the last `start` / `nav` at or before it), opened in a new tab.

## Privacy notice

While `enabled`, the About dialog (and `/privacy`) show:

> While enabled (through Oct 17), this site records your clicks and page views to help debug issues.

Discord draft (for the announcement):

> Heads-up: through Oct 17, the site keeps a short log of clicks, page views and errors (no file contents, no form text other than the path filter) so we can debug anything you report. It's deleted after 30 days.

## Overhead

Measured (see the commit): the logger is a separate chunk loaded only when on; the main bundle grows by the boot shim only. Per-event CPU: `log()` is an object push plus truncation (measured in `sessionLog.test.ts`'s budget check); JSON serialization happens once per flush.

## Enable (gcs)

1. On the gcs branch: copy `site/migrations/cw/0018_session_log.sql` to `site/migrations/gcs/0042_session_log.sql`; apply: `wrangler d1 migrations apply oa-gcs-usage-auth --remote` (Ryan's go).
2. `[vars]` in gcs's `wrangler.toml`: `SESSION_LOG_UNTIL = "2026-10-17"` (optionally `SESSION_LOG_RETAIN_DAYS = "30"`); deploy.
3. Check: `/api/session-log` → `{"enabled": true, …}` signed in; browse; `/admin/sessions` lists the session.
