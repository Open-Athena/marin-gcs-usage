/** The live ledger (marks + claims) and its head, straight from D1 — the
 * WAL every read applies on top of the scan (specs/view-serving.md §2). */
import type { Env } from './auth.js'
import type { KeepRow, OwnerRow } from './marks.js'

export interface Ledger {
  keepRows: KeepRow[]
  ownerRows: OwnerRow[]
  /** `MAX(actions.id)`: changes iff the ledger changed — the cache key for
   * anything folded from it. */
  head: number
}

// The rows at a head, per isolate: the head is one cheap query every reader
// re-checks, but the ~10k rows behind it were re-read per call — a lens
// series made that read once per scan.
let rowsMemo: { head: number; p: Promise<Ledger> } | null = null

export async function loadLedger(env: Env): Promise<Ledger> {
  const head = await ledgerHead(env)
  if (rowsMemo?.head === head) return rowsMemo.p
  const p = loadRows(env, head)
  rowsMemo = { head, p }
  p.catch(() => { if (rowsMemo?.p === p) rowsMemo = null })
  return p
}

async function loadRows(env: Env, head: number): Promise<Ledger> {
  const [keepRows, ownerRows] = await Promise.all([
    env.DB!.prepare(
      'SELECT k.prefix, k.keep, k.ts, a.actor AS who, a.id AS action_id ' +
      'FROM keep_prefixes k JOIN actions a ON a.id = k.action_id WHERE k.tombstoned IS NULL',
    ).all<KeepRow>(),
    env.DB!.prepare(
      'SELECT o.prefix, o.owner, o.ts, a.id AS action_id ' +
      'FROM owner_prefixes o JOIN actions a ON a.id = o.action_id WHERE o.tombstoned IS NULL',
    ).all<OwnerRow>(),
  ])
  return { keepRows: keepRows.results, ownerRows: ownerRows.results, head }
}

/** Just the head (one tiny query) — for cache keys before deciding whether a
 * full fold is needed. */
export async function ledgerHead(env: Env): Promise<number> {
  if (!env.DB) throw new Error('ledger backend not configured (DB)')
  const row = await env.DB.prepare('SELECT COALESCE(MAX(id), 0) AS head FROM actions').first<{ head: number }>()
  return row?.head ?? 0
}
