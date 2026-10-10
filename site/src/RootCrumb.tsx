/** The path bar's root crumb. Its label is the root's own name as the view's tree carries it (the deployment's
 *  `ROOT_LABEL`, `functions/_lib/view.ts`); with no tree — a refused filter view, a first paint — the same
 *  label from the deployment's config (`/api/filter-caps`), and only failing that the store's root scope word
 *  (`all buckets`), which also stands as the crumb's tooltip. */
import { Tooltip } from './Tooltip'

export function RootCrumb({ treeName, rootLabel, scopeWord, here, onClick }: {
  /** The view's root node's name, when a tree is here. */
  treeName?: string
  /** The deployment's `ROOT_LABEL`, once known. */
  rootLabel?: string
  /** The store's root scope word (`Store.rootLabel`). */
  scopeWord: string
  /** The page is at the root: the crumb is the current one. */
  here: boolean
  onClick: () => void
}) {
  return (
    <Tooltip content={scopeWord}>
      <button type="button" className={here ? 'here' : ''} onClick={onClick}>{treeName ?? rootLabel ?? scopeWord}</button>
    </Tooltip>
  )
}
