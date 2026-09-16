import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { MdAutoDelete } from 'react-icons/md'
import { describeRule, lifecycleDiff, rulePrefix } from './lifecycle'
import type { LifecycleRule } from './lifecycle'
import type { Store } from './stores'
import { Tooltip } from './Tooltip'

// The bucket's lifecycle rules as the scan job snapshotted them
// (`<store.base>/<scan>/lifecycle.json`), diffed against the previous scan's
// snapshot: a `<details>` fold under About. Scans from before the job started
// recording (2026-09-16) have no file — the fold says so rather than hiding.

const RECORDED_FROM = '2026-09-16'

/** One scan's rules; `null` = no snapshot for that scan (404 or any non-OK). */
function useLifecycle(store: Store, scan: string | null | undefined) {
  return useQuery<LifecycleRule[] | null>({
    queryKey: ['lifecycle', store.key, scan],
    enabled: !!scan,
    staleTime: Infinity,
    queryFn: async () => {
      const r = await fetch(`${store.base}/${scan}/lifecycle.json`)
      return r.ok ? ((await r.json()) as LifecycleRule[]) : null
    },
  })
}

export function LifecycleFold({ store, asof, prevScan, note }: {
  store: Store
  asof: string | null | undefined
  /** The scan before `asof` in the store's list (the diff baseline), if any. */
  prevScan: string | null | undefined
  /** One line under the table — where this deployment tracks the intended state. */
  note?: ReactNode
}) {
  const curQ = useLifecycle(store, asof)
  const prevQ = useLifecycle(store, prevScan)
  const rules = curQ.data
  const rows = rules ? lifecycleDiff(prevQ.data ?? null, rules) : []
  const loading = !!asof && curQ.isPending
  const title = rules ? `${rules.length} rule${rules.length === 1 ? '' : 's'}` : loading ? 'loading…' : 'no snapshot'
  return (
    <details className="prose fold lifecycle">
      <summary>
        <MdAutoDelete className="fold-icon" aria-hidden />
        <span><b>Bucket lifecycle</b> — {title}</span>
      </summary>
      {rules ? (
        <div className="tbl-scroll">
        <table className="worklist lifecycle-tbl">
          <thead>
            <tr><th>rule</th><th>prefix</th><th>action</th><th>status</th></tr>
          </thead>
          <tbody>
            {rows.map(({ rule, change, prev }) => (
              <tr key={`${rule.ID}:${change ?? ''}`} className={change === 'removed' ? 'removed' : undefined}>
                <td className="id">
                  {rule.ID}
                  {change === 'new' && <span className="chip new">new</span>}
                  {change === 'removed' && <span className="chip removed">removed</span>}
                  {change === 'changed' && prev && (
                    <Tooltip content={<>
                      Previous scan: <b>{describeRule(prev)}</b>
                      {prev.Status !== rule.Status && <> · {prev.Status}</>}
                      {rulePrefix(prev) !== rulePrefix(rule) && <> · <code>{rulePrefix(prev) || 'whole bucket'}</code></>}
                    </>}>
                      <span className="chip changed">changed</span>
                    </Tooltip>
                  )}
                </td>
                <td>{rulePrefix(rule) ? <code>{rulePrefix(rule)}</code> : <i>whole bucket</i>}</td>
                <td>{describeRule(rule)}</td>
                <td>{rule.Status}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : (
        <p className="tab-note">{loading ? 'loading…' : <>no snapshot for this scan (recorded from {RECORDED_FROM} on)</>}</p>
      )}
      {rules && prevScan && prevQ.data === null && <p className="tab-note">Changes aren’t shown: the previous scan has no snapshot.</p>}
      {note && <p className="tab-note">{note}</p>}
    </details>
  )
}
