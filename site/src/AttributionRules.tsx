import { useEffect } from 'react'
import type { TreeNode } from './types'

const GROUP_EMAIL = 'marin-gcs-usage@openathena.ai'
const DISCORD_URL = 'https://discord.com/channels/1354881461060243556/1412294350645493840'
const YAML_URL = 'https://github.com/Open-Athena/marin-gcs-usage/blob/gcs/marin/src/gcs_usage/identities.yaml'

/** Where owners come from, and where to go with questions. Claims are the
 *  source of truth; inferred attribution is only the bootstrap for whatever
 *  nobody has claimed yet — so this stays short and points at people, not at
 *  the rule tables (those live in identities.yaml for whoever wants them). */
export function AttributionRules({ tree }: { tree: TreeNode }) {
  // deep-linkable: the section mounts after data loads, so honor #attribution then
  useEffect(() => {
    if (location.hash === '#attribution') document.getElementById('attribution')?.scrollIntoView()
  }, [])

  const attributed = Object.entries(tree.tm ?? {})
    .filter(([t]) => t !== 'unattributed')
    .reduce((s, [, b]) => s + b, 0)
  const pct = ((100 * attributed) / tree.b).toFixed(1)
  return (
    <section className="attrib" id="attribution">
      <h2>Ownership</h2>
      <div className="prose">
        <p>
          An owner comes from one of two places. A <b>claim</b> — someone marking a prefix as theirs, from the
          map, the CLI, or the API — is the source of truth and always wins. Everything unclaimed falls back to
          <b> inferred</b> ownership (W&amp;B run configs, <code>.executor_info</code> sidecars, provenance records,
          and a short list of manual prefix rules in{' '}
          <a href={YAML_URL} target="_blank" rel="noreferrer"><code>identities.yaml</code></a>): “this user's runs
          wrote these bytes” — a starting point for finding your data, not a bill. Shared datasets and infra sit
          with a group, not a person. <b>{pct}%</b> of bytes have an owner today; the rest shows as{' '}
          <i>unattributed</i> (gray) until someone claims it.
        </p>
        <p className="feedback">
          Questions, a wrong owner, access for a teammate:{' '}
          <a href={`mailto:${GROUP_EMAIL}`}>{GROUP_EMAIL}</a> or{' '}
          <a href={DISCORD_URL} target="_blank" rel="noreferrer">#internal-discuss</a> on the Marin Discord.
        </p>
      </div>
    </section>
  )
}
