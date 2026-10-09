import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { applyToggle, BulkBar, caveats } from './BulkBar'
import { actionTargets, groupItems, type FilterCover } from './filterCover'

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
      '5 matches → 4 prefixes · 350 B',
      'review',
      'Each line is a folder whose every file matches, or a single match. Untick false positives — a whole folder’s worth, or one at a time.',
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
  it('assign sends folder prefixes only (files can\'t be owned alone); stage sends files and folders, never a bucket', () => {
    const items = [...c.items, { path: 'tomatoes-bucket', kind: 'dir' as const, b: 9, o: 1, roots: 1 }, { path: 'x/unk', kind: null, b: 1, o: 1, roots: 1 }]
    expect(actionTargets(items, 'gs://', 'assign')).toEqual({ prefixes: ['gs://marin-eu-west4/tomat/', 'gs://tomatoes-bucket/'], b: 59, o: 3, files: 3, buckets: 0, unknown: 1 })
    expect(actionTargets(items, 'gs://', 'stage')).toEqual({ prefixes: ['gs://marin-eu-west4/tomat/', ...clips.map(i => `gs://${i.path}`)], b: 350, o: 5, files: 0, buckets: 1, unknown: 1 })
  })
  it('says what it leaves out, in plain words', () => {
    expect(caveats({ files: 3, buckets: 0, unknown: 1 }, 'assign')).toEqual([
      '3 matching files sit in folders that also hold files that don’t match. Owners are set per folder, so assigning leaves them out.',
      '1 match couldn’t be checked (file or folder?) and is left out; open its folder to act on it.',
    ])
    expect(caveats({ files: 3, buckets: 2, unknown: 0 }, 'stage')).toEqual(['2 whole buckets can’t be staged; they are left out.'])
  })
})
