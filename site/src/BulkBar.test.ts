import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { BulkBar } from './BulkBar'

vi.mock('./owners', () => ({ useOwnerMutations: () => ({ post: { error: null } }) }))
vi.mock('./UserChip', () => ({ allUsers: () => [] }))
vi.mock('./units', () => ({ useUnits: () => ({ fmtBytes: (b: number) => `${b} B` }) }))

const render = (total: number, incomplete: boolean) => renderToStaticMarkup(createElement(BulkBar, {
  matches: [{ path: 'bucket/a', b: 10 }], total, incomplete, scheme: 'gs://', query: 'aaa',
}))

const expected = (count: string, warning: string) => [
  '<span class="bulkbar">',
  `<span class="bb-scope">${count} prefixes:</span>`,
  '<button type="button" class="act assign" disabled="">assign to me</button>',
  '<input list="bb-assign-users" placeholder="you" size="7" aria-label="Assign matches to user" value=""/>',
  '<datalist id="bb-assign-users"></datalist>',
  '<input placeholder="memo" size="10" aria-label="Bulk memo" value=""/>',
  `<span class="bb-warn">${warning}</span>`,
  '</span>',
].join('')

describe('bounded filter lists cannot bulk-assign their drawn subset', () => {
  it('uses the exact total even when only one match was drawn', () => {
    expect(render(89405, true)).toBe(expected('89,405', '&gt;5,000 matches — that&#x27;s a rule, not a gesture (use a `prefix_owners` glob)'))
  })

  it('blocks a bounded list even below the action limit', () => {
    expect(render(3, true)).toBe(expected('3', 'incomplete match list — narrow the filter before assigning'))
  })
})
