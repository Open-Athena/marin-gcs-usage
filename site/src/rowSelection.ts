import { useEffect, useRef } from 'react'
import { useRowSelection as useKbdRowSelection, useRowSelectionKeys as useKbdRowSelectionKeys, useActions } from 'use-kbd'
import type { RowSelectionRowProps, UseRowSelectionResult } from 'use-kbd'

// Multi-row selection for every table on the site (the /sweep console, the
// treemap's children table): use-kbd's `useRowSelection` — an anchor/cursor
// range plus pinned keys, resolved against `key(row)` so the selection
// survives paging and sort (the range freezes into pins when the page's rows
// change) — with the site's few conventions on top: rows are `.sel` / `.cur`,
// a click on a link, button or input inside a row doesn't select the row, the
// cursor row scrolls into view, and the header checkbox toggles the page
// without dropping rows pinned on other pages.

export interface RowSelection<T> extends UseRowSelectionResult<T> {
  cursorRow: T | undefined
  /** `<tr ref>` collector so the cursor row scrolls into view. */
  rowRef: (i: number) => (el: HTMLTableRowElement | null) => void
  /** Every row on the page is selected (the header checkbox). */
  pageAll: boolean
  togglePage: () => void
}

export function useRowSelection<T>(pageRows: readonly T[], key: (row: T) => string): RowSelection<T> {
  const sel = useKbdRowSelection(pageRows, key, { cursorClassName: 'cur', selectedClassName: 'sel' })
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([])
  useEffect(() => { rowRefs.current[sel.cursor]?.scrollIntoView({ block: 'nearest' }) }, [sel.cursor])
  const pageAll = pageRows.length > 0 && pageRows.every(sel.isSelected)
  const rowProps = (i: number): RowSelectionRowProps => {
    const p = sel.rowProps(i)
    return { ...p, onClick: e => { if (!(e.target as HTMLElement).closest('a, button, input, select')) p.onClick(e) } }
  }
  return {
    ...sel,
    rowProps,
    cursorRow: pageRows[sel.cursor],
    rowRef: i => el => { rowRefs.current[i] = el },
    pageAll,
    // Per-row toggles rather than `selectPage`, which replaces the pinned set
    // (and so would forget rows selected on other pages); the commit pins the
    // result and drops the cursor the last toggle left behind.
    togglePage: () => {
      pageRows.forEach((r, i) => { if (sel.isSelected(r) === pageAll) sel.toggle(i) })
      sel.commit()
    },
  }
}

/** use-kbd's selection bindings (j/k move, ⇧j/⇧k extend, …) under a
 *  ShortcutsModal group, with the site's page toggle on ⇧x in place of
 *  use-kbd's ⌃a select-page (which drops other pages' rows). `id` namespaces
 *  the action ids (`sweep`, `tbl`, …).
 *
 *  Esc clears the selection, but outside use-kbd: a capture-phase listener
 *  that acts only while something is selected and preventDefaults, so the
 *  treemap's Esc/Backspace drill-up (which skips a consumed key) stays put;
 *  with nothing selected the key falls through and drills up as usual.
 *  use-kbd can't express that — it preventDefaults a matched key before it
 *  checks `enabled`, and registering the action only while selected would
 *  re-render the whole app (registry version bump) on every empty ↔ non-empty
 *  transition. */
export function useRowSelectionKeys<T>(sel: RowSelection<T>, id: string, group: string) {
  useKbdRowSelectionKeys(sel, { idPrefix: id, group, bindings: { all: false, clear: false } })
  useActions({
    [`${id}:toggle-page`]: { label: 'Select / deselect every row on this page', group, defaultBindings: ['shift+x'], handler: sel.togglePage },
  })
  const selRef = useRef(sel)
  selRef.current = sel
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || selRef.current.count === 0) return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      e.preventDefault()
      selRef.current.clear()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])
}
