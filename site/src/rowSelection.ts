import { useCallback, useEffect, useRef, useState } from 'react'
import { useActions } from 'use-kbd'

// Headless multi-row selection, shared by every table on the site (the
// /sweep console, the treemap's children table). One model, the use-kbd
// table demo's: a `selected` set keyed by a stable row key (so it survives
// paging / sort / filter), and a `cursor` — a row index within the current
// page. Click selects just that row; ⌘/ctrl-click toggles one; shift-click
// and shift+j/k extend a range from the cursor; j/k move the cursor without
// touching the selection; `x` toggles the cursor row, ⇧x the page, Esc
// clears. Checkboxes are the additive mouse path.
//
// Slated to move upstream to use-kbd as `useRowSelection` (the file-tree
// session's read of this, 2026-09-09); keep the API shaped for that.

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

export interface RowSelection<T> {
  selected: ReadonlySet<string>
  isSelected: (row: T) => boolean
  /** Toggle (or set, with `on`) a batch of keys. */
  toggle: (keys: string[], on?: boolean) => void
  /** Replace the selection. */
  set: (keys: string[]) => void
  clear: () => void
  cursor: number
  setCursor: (i: number) => void
  cursorRow: T | undefined
  /** Move the cursor by `d` rows; `extend` selects the range crossed. */
  moveCursor: (d: number, extend: boolean) => void
  /** `<tr onClick>` handler implementing plain / shift / ⌘ clicks. */
  rowClick: (i: number, e: React.MouseEvent) => void
  /** `<tr ref>` collector so the cursor row scrolls into view. */
  rowRef: (i: number) => (el: HTMLTableRowElement | null) => void
  /** Row class names for the current state. */
  rowClass: (row: T, i: number) => string
  /** Every row on the page is selected (the header checkbox). */
  pageAll: boolean
  togglePage: () => void
}

export function useRowSelection<T>(pageRows: T[], key: (row: T) => string): RowSelection<T> {
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [cursor, setCursor] = useState(-1)
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([])
  useEffect(() => { rowRefs.current[cursor]?.scrollIntoView({ block: 'nearest' }) }, [cursor])

  const toggle = useCallback((keys: string[], on?: boolean) => setSelected(prev => {
    const next = new Set(prev)
    const add = on ?? !keys.every(k => prev.has(k))
    for (const k of keys) add ? next.add(k) : next.delete(k)
    return next
  }), [])
  const set = useCallback((keys: string[]) => setSelected(new Set(keys)), [])
  const clear = useCallback(() => { setSelected(new Set()); setCursor(-1) }, [])
  const moveCursor = (d: number, extend: boolean) => {
    if (!pageRows.length) return
    const from = cursor < 0 ? (d > 0 ? -1 : pageRows.length) : cursor
    const to = clamp(from + d, 0, pageRows.length - 1)
    if (extend) {
      const a = cursor < 0 ? to : cursor
      toggle(pageRows.slice(Math.min(a, to), Math.max(a, to) + 1).map(key), true)
    }
    setCursor(to)
  }
  const rowClick = (i: number, e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('a, button, input, select')) return
    if (e.shiftKey && cursor >= 0) toggle(pageRows.slice(Math.min(cursor, i), Math.max(cursor, i) + 1).map(key), true)
    else if (e.metaKey || e.ctrlKey) toggle([key(pageRows[i])])
    else set([key(pageRows[i])])
    setCursor(i)
  }
  const pageKeys = pageRows.map(key)
  const pageAll = pageRows.length > 0 && pageKeys.every(k => selected.has(k))
  return {
    selected,
    isSelected: row => selected.has(key(row)),
    toggle, set, clear,
    cursor, setCursor,
    cursorRow: pageRows[cursor],
    moveCursor, rowClick,
    rowRef: i => el => { rowRefs.current[i] = el },
    rowClass: (row, i) => [selected.has(key(row)) ? 'sel' : '', i === cursor ? 'cur' : ''].filter(Boolean).join(' '),
    pageAll,
    togglePage: () => toggle(pageKeys),
  }
}

/** The standard key bindings for a selection, under a use-kbd group. `id`
 *  namespaces the action ids (`sweep`, `tbl`, …). */
export function useRowSelectionKeys<T>(sel: RowSelection<T>, id: string, group: string, key: (row: T) => string) {
  useActions({
    [`${id}:down`]: { label: 'Cursor down', group, defaultBindings: ['j', 'arrowdown'], handler: () => sel.moveCursor(1, false) },
    [`${id}:up`]: { label: 'Cursor up', group, defaultBindings: ['k', 'arrowup'], handler: () => sel.moveCursor(-1, false) },
    [`${id}:extend-down`]: { label: 'Select down (extend from the cursor)', group, defaultBindings: ['shift+j', 'shift+arrowdown'], handler: () => sel.moveCursor(1, true) },
    [`${id}:extend-up`]: { label: 'Select up (extend from the cursor)', group, defaultBindings: ['shift+k', 'shift+arrowup'], handler: () => sel.moveCursor(-1, true) },
    [`${id}:toggle`]: { label: 'Select / deselect the cursor row', group, defaultBindings: ['x', 'space'], handler: e => { e?.preventDefault(); if (sel.cursorRow) sel.toggle([key(sel.cursorRow)]) } },
    [`${id}:toggle-page`]: { label: 'Select / deselect every row on this page', group, defaultBindings: ['shift+x'], handler: sel.togglePage },
    [`${id}:clear`]: { label: 'Clear the selection', group, defaultBindings: ['escape'], handler: sel.clear },
  })
}
