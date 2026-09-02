// POST /api/sweep/dispatch — launch a sweep executor run on GCP Batch from
// the /sweep console (specs/sweep-executor.md, "web dispatch bridge").
//
// Body: { mode: 'dry' | 'real', date: 'YYYY-MM-DD', buckets?: string[] }.
// Admin scope only. The submitted job runs the daily-snapshot image with the
// entrypoint overridden to `sweep manifest -S` (consuming the console's
// `sweep_approvals` sign-offs) followed by `sweep execute` — which re-lists,
// generation-matches, records to D1 (`deletion_runs`/`deletion_bands`, so the
// run surfaces in the console within its refetch window), and for `real`
// requires ≥7d soft delete on every bucket before deleting anything.
//
// Auth to GCP: a dedicated SA key (`gcs-usage-dispatch@…`, Batch-submit +
// actAs-job-SA ONLY — it cannot read or delete bucket data itself) stored as
// the `GCP_SA_KEY` Pages secret; we self-sign a JWT and exchange it for an
// access token (no SDK — Workers-compatible WebCrypto).
import { ADMIN_SCOPE, type Env as AuthEnv, json, requireScope } from '../../_lib/auth.js'

interface Env extends AuthEnv {
  GCP_SA_KEY?: string
}

const PROJECT = 'oa-internal-450019'
const REGION = 'us-central1'
const IMAGE = `us-central1-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/gcs-usage-snapshot:latest`
const JOB_SA = `gcs-usage-job@${PROJECT}.iam.gserviceaccount.com`
const CF_ACCOUNT_ID = '74981a43be0de7712369306c7b19133d'
const SECRET = (name: string) => `projects/${PROJECT}/secrets/${name}/versions/latest`

const b64url = (buf: ArrayBuffer | Uint8Array): string => {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  let s = ''
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** Service-account JWT → OAuth access token (cloud-platform scope). */
async function gcpToken(saKey: string): Promise<string> {
  const sa = JSON.parse(saKey) as { client_email: string; private_key: string }
  const pem = sa.private_key.replace(/-----[A-Z ]+-----|\s/g, '')
  const der = Uint8Array.from(atob(pem), c => c.charCodeAt(0))
  const key = await crypto.subtle.importKey(
    'pkcs8', der, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['sign'],
  )
  const now = Math.floor(Date.now() / 1000)
  const enc = new TextEncoder()
  const unsigned = `${b64url(enc.encode(JSON.stringify({ alg: 'RS256', typ: 'JWT' })))}.${b64url(enc.encode(JSON.stringify({
    iss: sa.client_email,
    scope: 'https://www.googleapis.com/auth/cloud-platform',
    aud: 'https://oauth2.googleapis.com/token',
    iat: now,
    exp: now + 3600,
  })))}`
  const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, enc.encode(unsigned))
  const r = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion: `${unsigned}.${b64url(sig)}`,
    }),
  })
  if (!r.ok) throw new Error(`token exchange: ${r.status} ${await r.text()}`)
  return ((await r.json()) as { access_token: string }).access_token
}

export const onRequestPost = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const gated = await requireScope(ctx, ADMIN_SCOPE)
  if (gated instanceof Response) return gated
  if (!ctx.env.GCP_SA_KEY) return json({ error: 'dispatch not configured (GCP_SA_KEY secret missing)' }, 503)

  const body = (await ctx.request.json().catch(() => null)) as
    | { mode?: string; date?: string; buckets?: string[] } | null
  const mode = body?.mode
  const date = body?.date
  if (mode !== 'dry' && mode !== 'real') return json({ error: "mode must be 'dry' or 'real'" }, 400)
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return json({ error: 'date must be YYYY-MM-DD (the plan scan)' }, 400)
  const buckets = body?.buckets ?? []
  if (buckets.some(b => !/^marin-[a-z0-9-]+$/.test(b))) return json({ error: 'bad bucket name' }, 400)

  const ts = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15).replace('T', '-').toLowerCase()
  const jobId = `gcs-sweep-${mode}-${ts}z`
  const plan = `gs://oa-gcs-usage-dvx/sweep/runs/${jobId}`
  const bflags = buckets.map(b => `-b ${b}`).join(' ')
  const script = [
    'set -euo pipefail',
    `gcs-usage sweep manifest -d "$SWEEP_DATE" -S ${bflags} -o "${plan}"`,
    `gcs-usage sweep execute ${bflags} ${mode === 'real' ? '--for-real ' : ''}"${plan}"`,
  ].join('\n')

  const spec = {
    taskGroups: [{
      taskCount: 1,
      taskSpec: {
        runnables: [{ container: { imageUri: IMAGE, entrypoint: '/bin/bash', commands: ['-c', script] } }],
        computeResource: { cpuMilli: 8000, memoryMib: 28000 },
        maxRetryCount: 0,
        maxRunDuration: '14400s',
        environment: {
          variables: {
            SWEEP_DATE: date,
            // `sweep execute` records the run's `actor` from $USER
            USER: gated.email ?? 'sweep-console',
            CLOUDFLARE_ACCOUNT_ID: CF_ACCOUNT_ID,
          },
          secretVariables: {
            GCS_USAGE_TOKEN: SECRET('gcs-sheet-sync-token'),
            CLOUDFLARE_API_TOKEN: SECRET('cf-pages-token'),
          },
        },
      },
    }],
    allocationPolicy: {
      instances: [{ policy: { machineType: 'n2-standard-8', bootDisk: { type: 'pd-balanced', sizeGb: '100' } } }],
      serviceAccount: { email: JOB_SA },
      location: { allowedLocations: [`regions/${REGION}`] },
    },
    logsPolicy: { destination: 'CLOUD_LOGGING' },
  }

  const token = await gcpToken(ctx.env.GCP_SA_KEY)
  const r = await fetch(
    `https://batch.googleapis.com/v1/projects/${PROJECT}/locations/${REGION}/jobs?job_id=${jobId}`,
    { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(spec) },
  )
  const out = await r.json().catch(() => ({}))
  if (!r.ok) return json({ error: 'batch submit failed', status: r.status, detail: out }, 502)
  return json({ job_id: jobId, mode, date, plan, by: gated.email })
}
