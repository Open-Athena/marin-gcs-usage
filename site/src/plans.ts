import { useMutation, useQueryClient } from '@tanstack/react-query'

// Client for the deletion-plan API (specs/sweep-plan-union.md, seam 1). The
// opt-in trash model: a trash gesture *stages* prefixes (any signed-in viewer
// may) into a shared open plan; an admin approves + dispatches from /staged.

export interface StageResult {
  plan_id: number
  staged: string[]
}

/** Stage prefixes for deletion (POST /api/plans/stage). Pass canonical
 *  `gs://…/` prefixes (trailing slash). Invalidates the plans query so /staged
 *  reflects the new items. */
export function useStage() {
  const qc = useQueryClient()
  return useMutation<StageResult, Error, string[]>({
    mutationFn: async (prefixes: string[]) => {
      const r = await fetch('/api/plans/stage', {
        method: 'POST',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ prefixes }),
      })
      if (!r.ok) throw new Error(((await r.json()) as { error?: string }).error ?? `${r.status}`)
      return r.json() as Promise<StageResult>
    },
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['plans'] }) },
  })
}
