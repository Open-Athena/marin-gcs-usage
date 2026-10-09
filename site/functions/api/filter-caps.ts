/**
 * `GET /api/filter-caps` — what the map's filter accepts on this deployment, for the filter box (its inline
 * refusal and help, `_lib/indexedOnly.ts`). No data, no gate: a deployment flag only.
 *
 *   → { indexedOnly: true }   (`FILTER_INDEXED_ONLY=1`: one literal substring of a name, unscoped)
 */
import { type Ctx, json } from '../_lib/auth.js'
import { indexedOnly } from '../_lib/indexedOnly.js'

export const onRequestGet = async (ctx: Ctx): Promise<Response> =>
  json({ indexedOnly: indexedOnly(ctx.env) }, 200, { 'cache-control': 'public, max-age=300' })
