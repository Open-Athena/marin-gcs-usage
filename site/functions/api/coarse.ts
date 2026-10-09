/** Dev-only exact basename aggregation; no canonical fallback or mutations. */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { askBox, boxFor, type BoxEnv } from '../_lib/queryBox.js'

const privateHeaders = { 'cache-control': 'private, no-store' }

export async function onRequestGet(ctx: Ctx & { env: Ctx['env'] & BoxEnv }): Promise<Response> {
  const identity = await requireViewer(ctx)
  if (identity instanceof Response) return identity
  if (ctx.env.QUERY_BOX_COARSE !== '1') return json({ error: 'Coarse preview is not enabled.' }, 404, privateHeaders)
  const url = new URL(ctx.request.url)
  for (const key of ['q', 'qs', 'lens', 'o', 'cl', 'by']) {
    if (url.searchParams.get(key)) return json({ error: 'Use a literal basename pattern without full-search or owner/class options.' }, 400, privateHeaders)
  }
  const base = boxFor(ctx.env, url, 'coarse')
  if (!base || url.searchParams.get('store')) return json({ error: 'Coarse preview serves the primary frozen index only.' }, 409, privateHeaders)
  const params = new URLSearchParams()
  for (const key of ['date', 'date0', 'name', 'mode', 'path', 'budget', 'levels']) {
    const value = url.searchParams.get(key)
    if (value != null) params.set(key, value)
  }
  const answer = await askBox(ctx.env, base, 'coarse', params, 1 << 20)
  if (answer.kind !== 'answer') {
    const unsupported = answer.why === '501', missing = answer.why === '409'
    return json({ error: unsupported ? 'Patterns matching directory names are not supported by this preview.' : missing ?
      'No frozen index covers this scan and path.' : `Coarse preview could not answer (${answer.why}). Narrow the directory or try a supported leaf-only pattern.` },
    unsupported ? 422 : missing ? 409 : 503, privateHeaders)
  }
  const headers = { ...privateHeaders, 'x-query-engine': answer.engine }
  if (answer.status !== 200) return json({ error: answer.body.trim() }, answer.status, headers)
  return new Response(answer.body, { status: answer.status, headers: {
    'content-type': 'application/json', ...headers,
  } })
}
