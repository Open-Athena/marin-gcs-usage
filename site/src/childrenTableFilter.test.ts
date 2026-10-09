import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TreeNode } from './types'
import type { CoverItem } from './filterCover'

// The table's own logic under a filter, with its surroundings stubbed: the selection is preset (`selected`),
// and the assign / trash controls render what they would send.
const { selected, staged } = vi.hoisted(() => ({ selected: new Set<string>(), staged: [] as string[][] }))
vi.mock('./auth', () => ({ useCanAssign: () => true, useCanStage: () => true }))
vi.mock('./store', () => ({ useStore: () => ({ staging: true, owners: true }) }))
vi.mock('./plans', () => ({ useStage: () => ({ error: null, mutate: (v: { prefixes: string[] }) => staged.push(v.prefixes) }) }))
vi.mock('./perf', () => ({ usePerfCommit: () => {} }))
vi.mock('./units', () => ({ useUnits: () => ({ fmtBytes: (b: number) => `${b} B` }) }))
vi.mock('use-prms', () => ({ intParam: () => ({}), useUrlState: () => [20, () => {}] }))
vi.mock('./Tooltip', () => ({ Tooltip: ({ content, children }: { content: ReactNode; children: ReactNode }) => createElement('span', { 'data-tip': typeof content === 'string' ? content : '' }, children) }))
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
const render = (filter?: { items: CoverItem[] | null; why?: string }) => renderToStaticMarkup(createElement(ChildrenTable, {
  node, segs: [], scheme: 'gs://', ownerIdx: { assignmentOf: () => null } as never, onOpen: () => {}, onOpenObject: () => {}, filter,
}))
const rows = (html: string) => [...(html.match(/<tbody>(.*?)<\/tbody>/)?.[1] ?? '').matchAll(/<tr[^>]*>(.*?)<\/tr>/g)].map(([, r]) => r)
const name = (row: string) => /<td class="prefix">(.*?)<\/td>/.exec(row)![1].replace(/<[^>]+>/g, '')
const tip = (row: string) => /<td class="prefix"><span data-tip="(.*?)"/.exec(row)?.[1]
const assigns = (html: string) => [...html.matchAll(/data-assign="([^"]*)" data-label="([^"]*)"/g)].map(([, p, l]) => [JSON.parse(p.replace(/&quot;/g, '"')), l])
const checkboxes = (html: string) => rows(html).map(r => /<td class="col-sel">(.*?)<\/td>/.exec(r)?.[1] === '' ? 'no box' : 'box')
const actionsCell = (row: string) => /<td class="actions">(.*)<\/td>$/.exec(row)?.[1] ?? ''

beforeEach(() => { selected.clear(); staged.length = 0 })

describe('the children table under a filter', () => {
  it('a row holding one match shows the path to it (as its tile), others their own name', () => {
    // At the root no name is elided, so no row carries the full-path tooltip (it would repeat the cell).
    expect(rows(render({ items })).map(r => [name(r), tip(r)])).toEqual([
      ['marin-us-central1/show', undefined],
      ['marin-eu-west4/tomat', undefined],
      ['bkt-none', undefined],
    ])
  })
  it('each row acts on its matches — a folder as its prefix, lone files as exact objects; a row with none listed has no checkbox and says why', () => {
    const html = render({ items })
    expect(assigns(html)).toEqual([
      [files, ''],
      [[{ key: 'gs://marin-eu-west4/tomat/', kind: 'prefix' }], ''],
    ])
    expect(checkboxes(html)).toEqual(['box', 'box', 'no box'])
    expect(rows(html).map(r => /data-tip="([^"]*)"><span class="none">—/.exec(actionsCell(r))?.[1] ?? null)).toEqual([
      null,
      null,
      'Listing this row’s matches…',
    ])
    expect(rows(html).map(r => /data-tip="(Stage[^"]*)"/.exec(actionsCell(r))?.[1] ?? null)).toEqual([
      'Stage this row’s matching 3 files for deletion (not the rest of the row) — an admin approves and dispatches from /staged',
      'Stage this row’s matching 1 folder for deletion (not the rest of the row) — an admin approves and dispatches from /staged',
      null,
    ])
  })
  it('the selection is the rows\' match roots, never their whole prefixes', () => {
    selected.add('gs://marin-eu-west4').add('gs://marin-us-central1')
    const html = render({ items })
    // The selection bar's assign and trash: the folder match and the three lone files, each with its kind.
    expect(assigns(html).at(-1)).toEqual([[...files, { key: 'gs://marin-eu-west4/tomat/', kind: 'prefix' }], 'assign 4…'])
    expect(/trash (\d+)<\/button>/.exec(html)?.[1]).toBe('4')
    expect(html.includes('gs://marin-eu-west4/&quot;') || html.includes('gs://marin-us-central1/&quot;')).toBe(false)
  })
  it('no row acts while the matches aren\'t listed (and says why)', () => {
    const html = render({ items: null, why: 'There are too many matches under this folder to list them. Open a folder below to act on its matches.' })
    expect([checkboxes(html), assigns(html)]).toEqual([['no box', 'no box', 'no box'], []])
    expect(rows(html).map(r => /data-tip="([^"]*)"/.exec(actionsCell(r))?.[1])).toEqual(Array(3).fill('There are too many matches under this folder to list them. Open a folder below to act on its matches.'))
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
