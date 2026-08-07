// CF Pages Function: serve the treemap's snapshot data live from the bucket.
//
// The app fetches /data/scans.json, /data/<date>/{tree,age,meta}.json, and
// /data/rules.json. These used to be static assets baked into every deploy —
// so the dashboard went stale whenever the daily job's `wrangler pages deploy`
// step failed (it had been broken since ~07-30). Now they're read live from
// gs://<bucket>/snapshots/ (the canonical store the snapshot job already
// writes), so new snapshots surface with no site redeploy.
//
// Same model as the /v1/files browser proxy: a read-only GCS HMAC key over the
// S3-compatible XML API, same-origin behind CF Access (no second sign-in, no
// CORS). CF Pages serves static assets before Functions, so this only works
// because public/data/ is no longer shipped (see the build).
import { S3Store } from '@rdub/file-tree/stores/s3'

interface Env {
  GCS_HMAC_KEY_ID: string
  GCS_HMAC_SECRET: string
}

const BUCKET = 'oa-gcs-usage-dvx'
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const CACHE = 'public, max-age=300' // daily cadence — ≤5min edge staleness is fine

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const { GCS_HMAC_KEY_ID, GCS_HMAC_SECRET } = ctx.env
  if (!GCS_HMAC_KEY_ID || !GCS_HMAC_SECRET) {
    return new Response('data proxy not configured (missing GCS HMAC creds)', { status: 503 })
  }
  const store = S3Store({
    endpoint: 'https://storage.googleapis.com', // GCS XML API is S3-compatible
    bucket: BUCKET,
    region: 'us-east1', // bucket location; GCS validates the SigV4 credential-scope region
    prefixes: ['snapshots/'], // allow-list: only the published snapshots
    accessKeyId: GCS_HMAC_KEY_ID,
    secretAccessKey: GCS_HMAC_SECRET,
  })
  const rel = new URL(ctx.request.url).pathname.replace(/^\/data\//, '')

  try {
    // scans.json → the date dirs directly under snapshots/ (newest-first)
    if (rel === 'scans.json') {
      const dates: string[] = []
      let cursor: string | undefined
      do {
        const page = await store.list('snapshots/', { cursor })
        for (const e of page.entries) {
          const d = e.key.replace(/^snapshots\//, '').replace(/\/$/, '')
          if (e.isDir && DATE_RE.test(d)) dates.push(d)
        }
        cursor = page.cursor
      } while (cursor)
      dates.sort().reverse()
      return new Response(JSON.stringify(dates), {
        headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': CACHE },
      })
    }
    // rules.json → snapshots/rules.json ; else /data/<date>/<file> → snapshots/<date>/<file>
    const key = rel === 'rules.json' ? 'snapshots/rules.json' : `snapshots/${rel}`
    const { bytes, contentType } = await store.get(key)
    return new Response(bytes, {
      headers: { 'content-type': contentType ?? 'application/json; charset=utf-8', 'cache-control': CACHE },
    })
  } catch {
    return new Response('not found', { status: 404 })
  }
}
