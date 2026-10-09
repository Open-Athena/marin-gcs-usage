import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { ActState } from './matchAct'
import type { Targets } from './filterCover'

// The tooltip's content inline (the real one mounts on hover), the assign select as a marker, the trash glyph
// as text: what's left is each state's exact markup.
vi.mock('./Tooltip', async () => {
  const { createElement: h } = await import('react')
  return { Tooltip: ({ content, children }: { content: ReactNode; children: ReactNode }) => h('span', { className: 'tt' }, children, h('span', { className: 'tt-tip' }, content)) }
})
vi.mock('./AssignSelect', async () => {
  const { createElement: h } = await import('react')
  return { AssignSelect: ({ label }: { label?: string }) => h('select', { 'aria-label': 'assign owner' }, label ?? 'assign…') }
})
vi.mock('react-icons/fa6', async () => {
  const { createElement: h } = await import('react')
  return { FaRegTrashCan: () => h('svg', null) }
})
const { RowActsView } = await import('./MatchActions')

const nop = () => {}
const act = { start: async () => {}, confirm: async () => {}, cancel: nop, undo: async () => {}, dismiss: nop, retry: async () => {} }
const t = (o: Partial<Targets>): Targets => ({ items: [], b: 0, o: 0, folders: 0, files: 0, buckets: 0, unknown: 0, ...o })
const render = (state: ActState, o: { warm?: 'cold' | 'warming' | 'ready'; confirmT?: Targets } = {}) => renderToStaticMarkup(createElement(RowActsView, {
  state, confirmT: o.confirmT ?? null, act, warm: o.warm ?? 'cold', canTrash: true, assigning: true, fmtBytes: (b: number) => `${b} B`,
}))

const TRASH = '<span class="tt"><button type="button" class="trash" aria-label="trash"><svg></svg></button><span class="tt-tip">Stage this row’s matches for deletion (not the rest of the row; listed when you click) — an admin approves and dispatches from /staged</span></span><select aria-label="assign owner">assign…</select>'
const box = (inner: string, live: string, attrs = '') => `<span class="mact"${attrs} tabindex="-1">${inner}<span class="act-live" role="status" aria-live="polite">${live}</span></span>`
const X = '<button type="button" class="act x" aria-label="dismiss">×</button>'
const SPIN = '<span class="spin" aria-hidden="true"></span>'
const req = { kind: 'stage' as const }
const OVER = 'Too many matches to act on at once (70,390); narrow the search or open a folder below. Agents can bulk-assign via the API.'

describe('a filtered row\'s controls, per state', () => {
  it('idle: trash and assign, offered at once (no "list" step); the live region empty', () => {
    expect(render({ s: 'idle' })).toBe(box(TRASH, ''))
  })
  it('prefetching (hovered): the same controls, busy', () => {
    expect(render({ s: 'idle' }, { warm: 'warming' })).toBe(box(TRASH, '').replace('<span class="mact"', '<span class="mact warming" aria-busy="true"'))
  })
  it('in progress: a spinner while listing and for one batch; a bar over several batches', () => {
    expect([
      render({ s: 'resolving', req }),
      render({ s: 'sending', req: { kind: 'assign', who: 'you' }, t: t({ items: [{ key: 'gs://b/x/', kind: 'prefix' }], folders: 1 }), batch: 0, batches: 1 }),
      render({ s: 'sending', req, t: t({ folders: 11_000 }), batch: 3, batches: 22 }),
    ]).toEqual([
      box('', `<span class="act-st busy">${SPIN}listing the matches…</span>`),
      box('', `<span class="act-st busy">${SPIN}assigning 1 folder…</span>`),
      box('', '<span class="act-st busy"><span class="act-bar" role="progressbar" aria-label="staging: batches sent" aria-valuemin="0" aria-valuemax="22" aria-valuenow="3"><i style="width:14%"></i></span>staging 3/22 batches</span>'),
    ])
  })
  it('confirm (a large set): what it sends, in how many batches, and what it leaves out', () => {
    const items = Array.from({ length: 1234 }, (_, i) => ({ key: `gs://b/f${i}`, kind: 'object' as const }))
    expect(render({ s: 'confirm', req, got: { items: [], complete: true } }, { confirmT: t({ items, files: 1234, b: 5000, o: 1234, unknown: 2 }) })).toBe(box('',
      '<span class="act-confirm">stage <b>1,234 files</b> (5000 B, 1,234 objects), sent in 3 batches?'
      + '<span class="act-note"> 2 matches couldn’t be checked (file or folder?) and are left out; open their folder to act on them.</span>'
      + '<button type="button" class="act go">confirm</button><button type="button" class="act">cancel</button></span>'))
  })
  it('accepted: what landed, an undo when there is one, a dismiss', () => {
    expect([
      render({ s: 'done', req: { kind: 'assign', who: 'you' }, text: 'assigned 1,234 files → you' }),
      render({ s: 'done', req, text: 'staged 3 folders', undo: { plan_id: 4, keys: ['gs://b/x/'] } }),
    ]).toEqual([
      box('', `<span class="act-st ok">✓ assigned 1,234 files → you</span>${X}`),
      box('', `<span class="act-st ok">✓ staged 3 folders</span><button type="button" class="act">undo</button>${X}`),
    ])
  })
  it('over the cap after resolving: the reason, muted, in the controls\' place (no red)', () => {
    expect(render({ s: 'muted', reason: OVER })).toBe(box('',
      `<span class="tt"><span class="act-st muted" tabindex="0">${OVER}</span><span class="tt-tip"><span class="bb-tip">${OVER}</span></span></span>${X}`))
  })
  it('failed (a 5xx, the network): red, with a retry', () => {
    expect(render({ s: 'failed', req: { kind: 'assign' }, msg: 'filter-cover: 503' })).toBe(box('',
      `<span class="act-st err">Can’t assign: filter-cover: 503</span><button type="button" class="act">retry</button>${X}`))
  })
})
