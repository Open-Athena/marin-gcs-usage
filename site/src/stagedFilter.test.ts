import { describe, expect, it } from 'vitest'
import { encodeSort, filterStaged, parseSort, stagedHaystack, type StagedFields } from './stagedFilter'

const NAMES: Record<string, string> = {
  'percy-liang': 'Percy',
  'chi-heem-wong': 'Chi-Heem',
  'will-held': 'Will',
  'ryan.williams@openathena.ai': 'Ryan',
  'david.hall@openathena.ai': 'David',
}
const name = (who: string) => NAMES[who] ?? who

const ROWS: StagedFields[] = [
  { prefix: 'gs://marin-us-central2/checkpoints/sft/mixture_sft_deeper_starling-de8fa2/', owners: ['chi-heem-wong'], stagedBy: 'ryan.williams@openathena.ai' },
  { prefix: 'gs://marin-us-central2/checkpoints/sft/tulu3_sft_spoonbill_945_lr_1e-04/', owners: ['percy-liang'], stagedBy: 'ryan.williams@openathena.ai' },
  { prefix: 'gs://marin-us-central2/checkpoints/ferry_qwen3_8b_pt_to_cooldown-1fc2cb/', owners: ['will-held'], stagedBy: 'ryan.williams@openathena.ai' },
  { prefix: 'gs://marin-eu-west4/ego-dex/', owners: [], stagedBy: 'ryan.williams@openathena.ai' },
  { prefix: 'gs://marin-us-central1/grug/tied_experts/d512/', owners: ['david.hall@openathena.ai'], stagedBy: 'david.hall@openathena.ai' },
  { prefix: 'gs://marin-us-central1/checkpoints/isoflop/isoflop-1e+19-d1024-nemotron/', owners: ['percy-liang', 'will-held'], stagedBy: 'ryan.williams@openathena.ai' },
]
const kept = (q: string) => {
  const r = filterStaged(ROWS, q, x => x, name)
  return r.error ? { error: r.error } : r.rows.map(x => x.prefix.replace(/^gs:\/\/marin-/, ''))
}

describe('filterStaged: the map syntax over prefix, owners and stager', () => {
  it('owners by display name or id: `percy|chi-heem`', () => {
    expect(kept('percy|chi-heem')).toEqual([
      'us-central2/checkpoints/sft/mixture_sft_deeper_starling-de8fa2/',
      'us-central2/checkpoints/sft/tulu3_sft_spoonbill_945_lr_1e-04/',
      'us-central1/checkpoints/isoflop/isoflop-1e+19-d1024-nemotron/',
    ])
  })

  it('AND across fields, NOT, globs, and the stager', () => {
    expect([kept('owner:will checkpoints -isoflop'), kept('will'), kept('sft -percy'), kept('staged-by:david'), kept('ego-*'), kept('')]).toEqual([
      ['us-central2/checkpoints/ferry_qwen3_8b_pt_to_cooldown-1fc2cb/'],
      // A bare `will` is a substring of `ryan.williams` too: every row Ryan staged.
      ROWS.filter(x => x.stagedBy.startsWith('ryan')).map(x => x.prefix.replace(/^gs:\/\/marin-/, '')),
      ['us-central2/checkpoints/sft/mixture_sft_deeper_starling-de8fa2/'],
      ['us-central1/grug/tied_experts/d512/'],
      ['eu-west4/ego-dex/'],
      ROWS.map(x => x.prefix.replace(/^gs:\/\/marin-/, '')),
    ])
  })

  it('a term never spans two fields; a too-short term is an error, not a silent empty set', () => {
    expect(stagedHaystack(ROWS[4], name).split('\n')).toEqual([
      'gs://marin-us-central1/grug/tied_experts/d512/',
      'owner:david.hall@openathena.ai David',
      'staged-by:david.hall@openathena.ai David',
    ])
    expect([kept('"d512/ owner"'), kept('"d512/"')]).toEqual([[], ['us-central1/grug/tied_experts/d512/']])
    expect(kept('ab')).toEqual({ error: expect.stringMatching(/3/) })
  })
})

describe('sort in the URL', () => {
  it('`-k` descending (default `-b` omitted), `k` ascending', () => {
    expect([parseSort(undefined), parseSort('-o'), parseSort('name'), encodeSort({ k: 'b', asc: false }), encodeSort({ k: 'staged', asc: true }), encodeSort({ k: 'd', asc: false })])
      .toEqual([{ k: 'b', asc: false }, { k: 'o', asc: false }, { k: 'name', asc: true }, undefined, 'staged', '-d'])
  })
})
