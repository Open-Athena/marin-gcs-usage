/** What the map's filter accepts on this deployment (`/api/filter-caps`), and the client's side of an
 *  indexed-only deployment (`functions/_lib/indexedOnly.ts`): its inline refusals, its one-form help, and
 *  the server's structured 400s turned into the filter box's message. */
import { useQuery } from '@tanstack/react-query'
import type { QuerySyntax } from '../functions/_lib/queryAst'
import { INDEXED_HELP } from '../functions/_lib/indexedOnly'
import { simple } from '../functions/_lib/querySyntax'

export interface FilterCaps { indexedOnly: boolean }

/** The deployment's filter capabilities (unloaded or unreachable: none, i.e. every form). */
export function useFilterCaps(): FilterCaps {
  const q = useQuery({
    queryKey: ['filter-caps'],
    queryFn: async (): Promise<FilterCaps> => {
      const r = await fetch('/api/filter-caps', { credentials: 'include' })
      if (!r.ok) return { indexedOnly: false }
      return await r.json() as FilterCaps
    },
    staleTime: Infinity,
    retry: 1,
  })
  return q.data ?? { indexedOnly: false }
}

/** The filter box's "syntax" on an indexed-only deployment: `simple`'s parse, the one-form help. */
export const INDEXED_SYNTAX: QuerySyntax = { id: simple.id, parse: q => simple.parse(q), describe: () => INDEXED_HELP }

/** The refusal codes an API answers a filter with (`functions/_lib/indexedOnly.ts`): a reason to show
 *  inline, never a failure to retry. */
const REFUSAL = /^(?:unsupported-[a-z-]+|scan-not-indexed|term-too-common)$/

/** A failed API response: `message` as `apiErrorMessage` words it; `refusal` the structured
 *  `{ error, code }` body's reason when its code is a filter refusal (`REFUSAL`). */
export class ApiError extends Error {
  constructor(message: string, readonly refusal?: { code: string; reason: string }) { super(message); this.name = 'ApiError' }
}

/** A failed API response as a throwable `ApiError`. */
export function apiError(status: number, text: string): ApiError {
  let refusal: ApiError['refusal']
  try {
    const j = JSON.parse(text) as { error?: unknown; code?: unknown }
    if (status === 400 && typeof j.error === 'string' && typeof j.code === 'string' && REFUSAL.test(j.code)) refusal = { code: j.code, reason: j.error }
  } catch { /* not JSON */ }
  return new ApiError(apiErrorMessage(status, text), refusal)
}

/** An error's filter refusal (a reason to state inline, with no retry or status), else null. */
export const refusalOf = (e: unknown): { code: string; reason: string } | null => (e instanceof ApiError && e.refusal) || null

/** A failed API response as an error message: a structured refusal (`{ error, code }`) as the filter box's
 *  `bad query: …` (shown under the box), anything else as its status and text. */
export function apiErrorMessage(status: number, text: string): string {
  try {
    const j = JSON.parse(text) as { error?: unknown; code?: unknown }
    if (status === 400 && typeof j.error === 'string' && typeof j.code === 'string') return `400: bad query: ${j.error}`
  } catch { /* not JSON */ }
  return `${status}: ${text.slice(0, 120)}`
}
