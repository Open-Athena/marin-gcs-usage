export interface CoarseCell {
  path: string
  label: string
  b: number
  o: number
  matches: number | null
  leaf: boolean
  folded?: boolean
  children?: CoarseCell[]
}

export interface CoarseBody {
  date: string
  name: string
  mode: 'exact' | 'contains' | 'suffix' | 'coverage'
  path: string
  tree: CoarseCell
  other: { b: number; o: number; matches: number | null }
  threshold: number
  seconds: number
  cacheHit: boolean
  levels: number
}

export interface CoarseDiff {
  before: CoarseBody
  after: CoarseBody
  delta: { b: number; o: number; matches: number | null }
  seconds: number
  cacheHit: boolean
}

const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
export function coarseError(status: number, body: string): Error {
  let parsed: unknown
  try { parsed = JSON.parse(body) } catch {
    // Gateway failures can be HTML/text; do not render their response markup.
    return new Error(`Coarse request failed (${status}).`)
  }
  return new Error(record(parsed) && typeof parsed.error === 'string' ? parsed.error : `Coarse request failed (${status}).`)
}
function integer(v: unknown): number {
  if (typeof v !== 'number' || !Number.isSafeInteger(v) || v < 0) throw new Error('Preview returned an unsupported integer total.')
  return v
}
function text(v: unknown): string {
  if (typeof v !== 'string') throw new Error('Preview returned an invalid path or name.')
  return v
}
function cell(v: unknown, coverage: boolean): CoarseCell {
  if (!record(v) || typeof v.leaf !== 'boolean') throw new Error('Preview returned an invalid tree cell.')
  return { path: text(v.path), label: text(v.label), b: integer(v.b), o: integer(v.o), matches: coverage ? null : integer(v.matches), leaf: v.leaf }
}

function partition(
  v: unknown,
  maxDepth: number,
  state: { nodes: number },
  coverage: boolean,
  depth = 0,
): { tree: CoarseCell; other?: { b: number; o: number; matches: number | null } } {
  if (++state.nodes > 2048 || depth > maxDepth) throw new Error('Preview exceeded its bounded tree size.')
  const root = cell(v, coverage)
  if (!record(v)) throw new Error('Preview returned an invalid tree cell.')
  if (depth > 0 && v.children === undefined && v.other === undefined) return { tree: root }
  if (!Array.isArray(v.children) || v.children.length > 256 || !record(v.other)) throw new Error('Preview returned an invalid child list.')
  const children = v.children.map(child => partition(child, maxDepth, state, coverage, depth + 1).tree)
  const other = { b: integer(v.other.b), o: integer(v.other.o), matches: coverage ? null : integer(v.other.matches) }
  if (!root.leaf && (children.reduce((n, c) => n + c.b, other.b) !== root.b ||
      children.reduce((n, c) => n + c.o, other.o) !== root.o || (!coverage && children.reduce((n, c) => n + c.matches!, other.matches!) !== root.matches))) {
    throw new Error('Preview child totals do not equal their parent.')
  }
  if (root.leaf && (children.length !== 0 || other.b !== 0 || other.o !== 0 || (!coverage && other.matches !== 0))) throw new Error('Preview returned an invalid leaf partition.')
  root.children = [...children, ...(other.b > 0 ? [{ path: root.path, label: '(other)', ...other, leaf: true, folded: true }] : [])]
  return { tree: root, other }
}

export function parseCoarse(v: unknown): CoarseBody {
  if (!record(v) || (v.schema !== 'coarse-v1' && v.schema !== 'coarse-tree-v1' && v.schema !== 'coverage-v1') || v.exact !== true || v.incremental !== false || !record(v.tree)) {
    throw new Error('Preview returned an unsupported response.')
  }
  const coverage = v.schema === 'coverage-v1'
  const levels = v.schema === 'coarse-tree-v1' ? integer(v.levels) : 1
  if (levels < 1 || levels > 4) throw new Error('Preview exceeded its bounded tree depth.')
  const { tree: root, other } = partition(v.tree, levels, { nodes: 0 }, coverage)
  const mode = coverage ? 'coverage' : v.mode
  if (mode !== 'exact' && mode !== 'contains' && mode !== 'suffix' && mode !== 'coverage') throw new Error('Preview returned an unsupported predicate mode.')
  if (!coverage && mode === 'coverage') throw new Error('Preview returned an unsupported predicate mode.')
  if (!other) throw new Error('Preview returned an invalid root partition.')
  if (typeof v.server_response_s !== 'number' || !Number.isFinite(v.server_response_s) || v.server_response_s < 0 || typeof v.cache_hit !== 'boolean') {
    throw new Error('Preview returned invalid timing metadata.')
  }
  return { date: text(v.date), name: text(coverage ? v.pattern : v.name), mode, path: text(v.path), tree: root, other, threshold: integer(v.threshold_bytes),
    seconds: v.server_response_s, cacheHit: v.cache_hit, levels }
}

export function parseCoarseDiff(v: unknown): CoarseDiff {
  if (!record(v) || (v.schema !== 'coarse-diff-v1' && v.schema !== 'coarse-tree-diff-v1' && v.schema !== 'coverage-diff-v1') || v.exact !== true || v.incremental !== false || !record(v.delta)) {
    throw new Error('Preview returned an unsupported diff response.')
  }
  const before = parseCoarse(v.before), after = parseCoarse(v.after)
  const coverage = v.schema === 'coverage-diff-v1'
  if (coverage !== (before.mode === 'coverage') || coverage !== (after.mode === 'coverage')) throw new Error('Preview diff contracts do not agree.')
  const aligned = (a: CoarseCell, b: CoarseCell): boolean => {
    const ac = a.children?.filter(c => !c.folded) ?? [], bc = b.children?.filter(c => !c.folded) ?? []
    return a.path === b.path && a.leaf === b.leaf && Boolean(a.children) === Boolean(b.children) && ac.length === bc.length && ac.every((c, i) => aligned(c, bc[i]))
  }
  if (before.path !== after.path || before.name !== after.name || before.mode !== after.mode || before.levels !== after.levels || before.threshold !== after.threshold ||
      !aligned(before.tree, after.tree)) throw new Error('Preview diff partitions do not align.')
  const delta = { b: after.tree.b - before.tree.b, o: after.tree.o - before.tree.o, matches: coverage ? null : after.tree.matches! - before.tree.matches! }
  if (v.delta.b !== delta.b || v.delta.o !== delta.o || (!coverage && v.delta.matches !== delta.matches)) throw new Error('Preview diff totals are inconsistent.')
  if (typeof v.server_response_s !== 'number' || !Number.isFinite(v.server_response_s) || v.server_response_s < 0 || typeof v.cache_hit !== 'boolean') {
    throw new Error('Preview returned invalid timing metadata.')
  }
  return { before, after, delta, seconds: v.server_response_s, cacheHit: v.cache_hit }
}
