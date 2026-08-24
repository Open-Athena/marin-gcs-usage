/**
 * `GET /api/todo?limit=&min_frac=` — the keep-axis review backlog: the largest
 * prefixes with no keep/sweep decision anywhere in their subtree or ancestry
 * (specs/actions-ledger.md). The single source of truth the UI's To-do view and
 * `gcs-usage todo` both consume.
 *
 *   → { scan, min_bytes, count, items: [{ prefix, bytes, objects }] }
 *
 * `prefix` is the gs:// dir form, ready to hand straight to `gcs-usage mark`.
 */
import { S3Store } from '@rdub/file-tree/stores/s3'
import { type Ctx, type Env, GCS_SCOPE, json, requireScope } from '../_lib/auth.js'
import { keepSets, type Node, todoItems } from '../_lib/todo.js'

const BUCKET = 'oa-gcs-usage-dvx'
const DATE_RE = /^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/
const DEFAULT_LIMIT = 100
const DEFAULT_MIN_FRAC = 0.0005 // ignore dust; overridable via ?min_frac=

async function latestScan(store: ReturnType<typeof S3Store>): Promise<string | null> {
  const dates: string[] = []
  let cursor: string | undefined
  do {
    const page = await store.list('snapshots/', { cursor })
    for (const e of page.entries) {
      const d = e.key.slice('snapshots/'.length).replace(/\/$/, '')
      if (e.isDir && DATE_RE.test(d)) dates.push(d)
    }
    cursor = page.cursor
  } while (cursor)
  dates.sort()
  return dates.length ? dates[dates.length - 1] : null
}

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const { request, env } = ctx as { request: Request; env: Env }
  if (!env.DB) return json({ error: 'todo backend not configured (DB)' }, 503)
  const { GCS_HMAC_KEY_ID, GCS_HMAC_SECRET } = env
  if (!GCS_HMAC_KEY_ID || !GCS_HMAC_SECRET) return json({ error: 'data proxy not configured (HMAC)' }, 503)
  const id = await requireScope(ctx, GCS_SCOPE)
  if (id instanceof Response) return id

  const url = new URL(request.url)
  const limit = Math.min(Math.max(Number(url.searchParams.get('limit')) || DEFAULT_LIMIT, 1), 1000)
  const minFrac = Number(url.searchParams.get('min_frac')) || DEFAULT_MIN_FRAC

  const store = S3Store({
    endpoint: 'https://storage.googleapis.com',
    bucket: BUCKET,
    region: 'us-east1',
    prefixes: ['snapshots/'],
    accessKeyId: GCS_HMAC_KEY_ID,
    secretAccessKey: GCS_HMAC_SECRET,
  })

  const scan = await latestScan(store)
  if (!scan) return json({ error: 'no published scan' }, 404)

  const [{ bytes }, decidedRows] = await Promise.all([
    store.get(`snapshots/${scan}/tree.json`),
    env.DB.prepare(
      `SELECT prefix FROM (
         SELECT prefix, keep,
           ROW_NUMBER() OVER (PARTITION BY prefix ORDER BY ts DESC, action_id DESC) AS rn
         FROM keep_prefixes WHERE tombstoned IS NULL
       ) WHERE rn = 1 AND keep IS NOT NULL`,
    ).all<{ prefix: string }>(),
  ])

  const tree = JSON.parse(new TextDecoder().decode(bytes)) as Node & { b: number }
  const minBytes = Math.floor(tree.b * minFrac)
  const { decided, hasBelow } = keepSets((decidedRows.results ?? []).map(r => r.prefix))
  const items = todoItems(tree.c ?? [], decided, hasBelow, minBytes, limit)

  return json({ scan, min_bytes: minBytes, count: items.length, items })
}
