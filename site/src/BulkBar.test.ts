import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { applyToggle, BulkBar, caveats } from './BulkBar'
import { actionTargets, groupItems, type FilterCover, targetsText } from './filterCover'

vi.mock('./owners', () => ({ useOwnerMutations: () => ({ post: { error: null } }) }))
vi.mock('./plans', () => ({ useStage: () => ({ error: null }) }))
vi.mock('./UserChip', () => ({ allUsers: () => [] }))
vi.mock('./units', () => ({ useUnits: () => ({ fmtBytes: (b: number) => `${b} B` }) }))

// The Adam_Carolla `tomat` shape: every match a file directly in one folder that also holds other files (no
// collapse), plus a folder elsewhere every file of which matches (collapsed to one prefix).
const SHOW = 'marin-us-central1/podcast_audio_top1000_60s_clips/Adam_Carolla_Show'
const clips = Array.from({ length: 3 }, (_, i) => ({ path: `${SHOW}/ACS_Rotten_Tomatoes_chunk00${i}.mp3`, kind: 'file' as const, b: 100, o: 1, roots: 1 }))
const cover = (o: Partial<FilterCover> = {}): FilterCover => ({
  date: '2026-10-09', path: '', q: 'tomat', complete: true, looked: 2, unchecked: 0,
  roots: { n: 5, b: 300 + 50, o: 5 },
  items: [{ path: 'marin-eu-west4/tomat', kind: 'dir', b: 50, o: 2, roots: 2 }, ...clips],
  ...o,
})
const render = (c: FilterCover, opts: { canAssign?: boolean; canStage?: boolean } = {}) =>
  renderToStaticMarkup(createElement(BulkBar, { cover: c, scheme: 'gs://', query: 'tomat', canAssign: opts.canAssign ?? true, canStage: opts.canStage ?? true }))
const text = (html: string) => html.replace(/<[^>]+>/g, '|').split('|').map(s => s.trim()).filter(Boolean)

describe('the bulk bar lists the matches as prefixes, grouped by folder, for review', () => {
  it('summarizes, groups heaviest folder first, and offers assign + stage (no cap, no jargon)', () => {
    expect(text(render(cover()))).toEqual([
      '5 matches → 1 folder, 3 files · 350 B',
      'review',
      'Each line is a folder whose every file matches (sent as that folder), or a single matching file (sent as exactly that file — never its neighbours). Untick false positives — a whole folder’s worth, or one at a time.',
      'all', 'none',
      `${SHOW}/`, '3/3 · 300 B',
      'ACS_Rotten_Tomatoes_chunk000.mp3', '100 B',
      'ACS_Rotten_Tomatoes_chunk001.mp3', '100 B',
      'ACS_Rotten_Tomatoes_chunk002.mp3', '100 B',
      'marin-eu-west4/', '50 B',
      'tomat/', '2 files · 50 B',
      'assign to me', 'stage for deletion',
    ])
  })
  it('an incomplete list says why, in plain words, and offers nothing', () => {
    expect(text(render(cover({ complete: false, items: [], reason: 'There are too many matches under this folder to list them. Open a folder below to act on its matches.' })))).toEqual([
      '5 matches:', 'There are too many matches under this folder to list them. Open a folder below to act on its matches.',
    ])
  })
  it('nothing to offer a viewer who can neither assign nor stage', () => {
    expect(render(cover(), { canAssign: false, canStage: false })).toBe('')
  })
})

describe('deselect: a folder\'s worth or one item, and what each action then sends', () => {
  const c = cover()
  it('groups by parent folder, heaviest first', () => {
    expect(groupItems(c.items).map(g => [g.parent, g.items.map(i => i.path.split('/').pop()), g.b, g.o])).toEqual([
      [SHOW, ['ACS_Rotten_Tomatoes_chunk000.mp3', 'ACS_Rotten_Tomatoes_chunk001.mp3', 'ACS_Rotten_Tomatoes_chunk002.mp3'], 300, 3],
      ['marin-eu-west4', ['tomat'], 50, 2],
    ])
  })
  it('dropping a folder\'s group leaves the rest; dropping one item leaves its siblings; ticking restores', () => {
    const kept = (d: ReadonlySet<string>) => c.items.filter(i => !d.has(i.path)).map(i => i.path)
    const noShow = applyToggle(new Set(), groupItems(c.items)[0].items.map(i => i.path), true)
    expect(kept(noShow)).toEqual(['marin-eu-west4/tomat'])
    const noOne = applyToggle(new Set(), [clips[1].path], true)
    expect(kept(noOne)).toEqual(['marin-eu-west4/tomat', clips[0].path, clips[2].path])
    expect(kept(applyToggle(noOne, [clips[1].path], false))).toEqual(c.items.map(i => i.path))
  })
  it('a folder goes as its prefix, a lone file as that exact object (never `key/`); stage never a bucket', () => {
    const items = [...c.items, { path: 'tomatoes-bucket', kind: 'dir' as const, b: 9, o: 1, roots: 1 }, { path: 'x/unk', kind: null, b: 1, o: 1, roots: 1 }]
    const files = clips.map(i => ({ key: `gs://${i.path}`, kind: 'object' }))
    expect(actionTargets(items, 'gs://', 'assign')).toEqual({
      items: [{ key: 'gs://marin-eu-west4/tomat/', kind: 'prefix' }, ...files, { key: 'gs://tomatoes-bucket/', kind: 'prefix' }],
      b: 359, o: 6, folders: 2, files: 3, buckets: 0, unknown: 1,
    })
    expect(actionTargets(items, 'gs://', 'stage')).toEqual({
      items: [{ key: 'gs://marin-eu-west4/tomat/', kind: 'prefix' }, ...files],
      b: 350, o: 5, folders: 1, files: 3, buckets: 1, unknown: 1,
    })
    expect(targetsText(actionTargets(items, 'gs://', 'stage'))).toBe('1 folder, 3 files')
    expect(targetsText({ folders: 0, files: 1 })).toBe('1 file')
  })
  it('says what it leaves out, in plain words (lone files are no longer left out)', () => {
    expect(caveats({ buckets: 0, unknown: 1 }, 'assign')).toEqual([
      '1 match couldn’t be checked (file or folder?) and is left out; open its folder to act on it.',
    ])
    expect(caveats({ buckets: 2, unknown: 0 }, 'stage')).toEqual([
      '2 whole buckets can’t be staged; they are left out.',
    ])
  })
})

describe('the matches are listed on demand', () => {
  const bar = (p: Record<string, unknown>) => text(renderToStaticMarkup(createElement(BulkBar, { scheme: 'gs://', query: 'tomat', canAssign: true, canStage: true, ...p })))
  it('not yet asked for: one button that asks (no count, no actions); asked for: the listing, then the bar', () => {
    expect([bar({ onWant: () => {} }), bar({ loading: true }), bar({ cover: cover() }).slice(0, 2), bar({ onWant: () => {}, canAssign: false, canStage: false })]).toEqual([
      ['act on the matches…'],
      ['listing the matches…'],
      ['5 matches → 1 folder, 3 files · 350 B', 'review'],
      [],
    ])
  })
})

