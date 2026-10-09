import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TreeNode } from './types'
import type { CoverItem } from './filterCover'

// The table's own logic under a filter, with its surroundings stubbed: the selection is preset (`selected`),
// and the assign / trash controls render what they would send.
const { selected, staged, acts } = vi.hoisted(() => ({ selected: new Set<string>(), staged: [] as string[][], acts: [] as { resolve: () => Promise<unknown> }[] }))
vi.mock('./auth', () => ({ useCanAssign: () => true, useCanStage: () => true }))
vi.mock('./store', () => ({ useStore: () => ({ staging: true, owners: true }) }))
vi.mock('./plans', () => ({ useStage: () => ({ error: null, mutate: (v: { prefixes: string[] }) => staged.push(v.prefixes) }) }))
vi.mock('./perf', () => ({ usePerfCommit: () => {} }))
vi.mock('./units', () => ({ useUnits: () => ({ fmtBytes: (b: number) => `${b} B` }) }))
vi.mock('use-prms', () => ({ intParam: () => ({}), useUrlState: () => [20, () => {}] }))
vi.mock('./Tooltip', () => ({ Tooltip: ({ content, children }: { content: ReactNode; children: ReactNode }) => createElement('span', { 'data-tip': typeof content === 'string' ? content : '' }, children) }))
// A filtered row's controls as a marker naming what it acts on; the selection's action records its deps.
vi.mock('./MatchActions', async () => {
  const { createElement: h } = await import('react')
  return {
    RowActs: ({ path, src }: { path: string; src: { key: string } }) => h('i', { 'data-row': path, 'data-src': src.key }),
    ActStatus: () => h('span', { className: 'act-live' }),
    useActDeps: () => ({}),
    useKeepFocus: () => ({ ref: () => {}, handlers: {} }),
    useMatchAct: (deps: { resolve: () => Promise<unknown> }) => { acts.push(deps); return { state: { s: 'idle' }, confirmT: null, start: async () => {} } },
  }
})
vi.mock('./AssignSelect', () => ({ AssignSelect: ({ items, label }: { items: unknown; label?: string }) => createElement('i', { 'data-assign': JSON.stringify(items), 'data-label': label ?? '' }) }))
vi.mock('./rowSelection', () => ({
  useRowSelectionKeys: () => {},
  useRowSelection: <T,>(rows: readonly T[], key: (r: T) => string) => ({
    selected, isSelected: (r: T) => selected.has(key(r)), toggle: () => {}, rowRef: () => () => {}, rowProps: () => ({}),
    pageAll: false, togglePage: () => {}, clear: () => {}, cursor: 0, cursorRow: rows[0],
  }),
}))
const { ChildrenTable } = await import('./ChildrenTable')

// The fleet root under `?f=tomat`: `marin-eu-west4` holds one match (`tomat`, drawn as a chain), the show
// folder 3 matching files among others (drawn as its own subtree), `bkt-none` no listed match.
const node: TreeNode = {
  n: 'all', k: 'dir', b: 0, o: 0, c: [
    { n: 'marin-eu-west4', k: 'dir', b: 50, o: 2, c: [{ n: 'tomat', k: 'dir', b: 50, o: 2, c: [{ n: 'a', k: 'file', b: 20, o: 1 }, { n: 'b', k: 'file', b: 30, o: 1 }] }] },
    { n: 'marin-us-central1', k: 'dir', b: 300, o: 3, c: [{ n: 'show', k: 'dir', b: 300, o: 3, c: [{ n: 'x1.mp3', k: 'file', b: 100, o: 1 }, { n: 'x2.mp3', k: 'file', b: 100, o: 1 }, { n: 'x3.mp3', k: 'file', b: 100, o: 1 }] }] },
    { n: 'bkt-none', k: 'dir', b: 7, o: 1 },
  ],
}
const items: CoverItem[] = [
  { path: 'marin-eu-west4/tomat', kind: 'dir', b: 50, o: 2, roots: 1 },
  ...[1, 2, 3].map(i => ({ path: `marin-us-central1/show/x${i}.mp3`, kind: 'file' as const, b: 100, o: 1, roots: 1 })),
]
const files = [1, 2, 3].map(i => ({ key: `gs://marin-us-central1/show/x${i}.mp3`, kind: 'object' }))
/** The filter's row source: `resolve` slices `items` to the row (recording each row asked for). */
const asked: string[] = []
const src = (items: CoverItem[]) => ({
  key: 'K', prefetch: async () => {},
  resolve: async (row: string) => { asked.push(row); return { items: items.filter(i => i.path === row || i.path.startsWith(`${row}/`)), complete: true } },
})
const render = (filter?: ReturnType<typeof src>) => renderToStaticMarkup(createElement(ChildrenTable, {
  node, segs: [], scheme: 'gs://', ownerIdx: { assignmentOf: () => null } as never, onOpen: () => {}, onOpenObject: () => {}, filter,
}))
const rows = (html: string) => [...(html.match(/<tbody>(.*?)<\/tbody>/)?.[1] ?? '').matchAll(/<tr[^>]*>(.*?)<\/tr>/g)].map(([, r]) => r)
const name = (row: string) => /<td class="prefix">(.*?)<\/td>/.exec(row)![1].replace(/<[^>]+>/g, '')
const tip = (row: string) => /<td class="prefix"><span data-tip="(.*?)"/.exec(row)?.[1]
const assigns = (html: string) => [...html.matchAll(/data-assign="([^"]*)" data-label="([^"]*)"/g)].map(([, p, l]) => [JSON.parse(p.replace(/&quot;/g, '"')), l])
const checkboxes = (html: string) => rows(html).map(r => /<td class="col-sel">(.*?)<\/td>/.exec(r)?.[1] === '' ? 'no box' : 'box')
const actionsCell = (row: string) => /<td class="actions">(.*)<\/td>$/.exec(row)?.[1] ?? ''

beforeEach(() => { selected.clear(); staged.length = 0; acts.length = 0; asked.length = 0 })

describe('the children table under a filter', () => {
  it('a row holding one match shows the path to it (as its tile), others their own name', () => {
    // At the root no name is elided, so no row carries the full-path tooltip (it would repeat the cell).
    expect(rows(render(src(items))).map(r => [name(r), tip(r)])).toEqual([
      ['marin-us-central1/show', undefined],
      ['marin-eu-west4/tomat', undefined],
      ['bkt-none', undefined],
    ])
  })
  it('every row offers its actions at once over its own path (no "list" step), and every row is selectable', () => {
    const html = render(src(items))
    expect(rows(html).map(r => [/data-row="([^"]*)" data-src="([^"]*)"/.exec(actionsCell(r))?.slice(1)])).toEqual([
      [['marin-us-central1', 'K']],
      [['marin-eu-west4', 'K']],
      [['bkt-none', 'K']],
    ])
    expect([checkboxes(html), assigns(html), html.includes('>list<')]).toEqual([['box', 'box', 'box'], [], false])
  })
  it('the selection acts on the selected rows\' matches (resolved on the click), never their whole prefixes', async () => {
    selected.add('gs://marin-eu-west4').add('gs://marin-us-central1')
    const html = render(src(items))
    // The bar: the count, then trash and assign over the rows' matches (no per-match count before the click).
    expect(/<div class="sel-bar-dock">(.*)<\/div><\/section>$/.exec(html)![1].replace(/<svg.*?<\/svg>/, '<svg/>')).toBe(
      '<span class="sel-bar"><b>2</b> selected · 350 B<span class="acts"><span class="mact sel-acts" tabindex="-1">'
      + '<span data-tip="Optional: one note for this deletion — why these matches go. Stored with the batch, visible to the admin who dispatches."><input class="memo" placeholder="note (optional)" aria-label="deletion note" value=""/></span>'
      + '<span data-tip=""><button type="button" class="trash" aria-label="trash selected"><svg/> trash</button></span><i data-label="assign 2…"></i><span class="act-live"></span></span>'
      + '<button type="button" class="quiet">deselect</button></span></span>')
    const got = await acts.at(-1)!.resolve()
    expect([asked, got]).toEqual([
      ['marin-us-central1', 'marin-eu-west4'],
      { items: [...items.slice(1), items[0]], complete: true },
    ])
  })
  it('without a filter every row acts on its own prefix, named as itself', () => {
    selected.add('gs://bkt-none')
    const html = render()
    expect(rows(html).map(name)).toEqual(['marin-us-central1', 'marin-eu-west4', 'bkt-none'])
    const pre = (key: string) => ({ key, kind: 'prefix' })
    expect(assigns(html)).toEqual([
      [pre('gs://marin-us-central1/'), ''], [pre('gs://marin-eu-west4/'), ''], [pre('gs://bkt-none/'), ''], [[pre('gs://bkt-none/')], 'assign 1…'],
    ])
  })
  it('without a filter a file row acts on that exact object (never `key/`), a folder row on its prefix', () => {
    const show = (node.c![1].c![0])
    const html = renderToStaticMarkup(createElement(ChildrenTable, {
      node: { ...show, c: [...show.c!, { n: 'sub', k: 'dir', b: 1, o: 1 }] }, segs: ['marin-us-central1', 'show'], scheme: 'gs://',
      ownerIdx: { assignmentOf: () => null } as never, onOpen: () => {}, onOpenObject: () => {},
    }))
    expect(assigns(html)).toEqual([
      ...files.map(f => [f, '']),
      [{ key: 'gs://marin-us-central1/show/sub/', kind: 'prefix' }, ''],
    ])
    expect(rows(html).map(r => /data-tip="(Stage[^"]*)"/.exec(actionsCell(r))?.[1])).toEqual([
      ...Array(3).fill('Stage this file for deletion (exactly this object) — an admin approves and dispatches from /staged'),
      'Stage this folder for deletion — an admin approves and dispatches from /staged',
    ])
  })
})
