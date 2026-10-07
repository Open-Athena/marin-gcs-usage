/** Protected opt-in root summaries, never canonical TM or a scan fallback. */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { askHotL1, hotParams, HotQueryError, type HotL1Env, privateHeaders } from '../_lib/hotL1.js'

export async function onRequest(ctx: Ctx & { env: HotL1Env }): Promise<Response> {
  // Do not inherit the anonymous viewer bypass of a public deployment.
  const identity = await requireViewer({ request: ctx.request, env: { ...ctx.env, PUBLIC_READ: undefined } })
  if (identity instanceof Response) {
    const headers = new Headers(identity.headers)
    headers.set('cache-control', privateHeaders['cache-control'])
    return new Response(identity.body, { status: identity.status, headers })
  }
  if (ctx.env.PUBLIC_READ || identity.via === 'public') return json({ error: 'Hot L1 requires a private viewer-protected deployment.' }, 403, privateHeaders)
  if (ctx.env.QUERY_BOX_HOT_L1 !== '1') return json({ error: 'Hot L1 is not enabled.' }, 404, privateHeaders)
  if (ctx.request.method !== 'GET') return json({ error: 'Only GET is supported.' }, 405, { ...privateHeaders, allow: 'GET' })
  if (ctx.env.STORE_KEY !== undefined || ctx.env.STORE_SCOPE !== undefined) return json({ error: 'Hot L1 serves the primary frozen index only.' }, 409, privateHeaders)
  let params: URLSearchParams
  try {
    params = hotParams(new URL(ctx.request.url))
  } catch (error) {
    if (!(error instanceof HotQueryError)) throw error
    return json({ error: error.message }, 400, privateHeaders)
  }
  return askHotL1(ctx.env, params)
}

export const onRequestGet = onRequest
