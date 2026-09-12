// GCP auth + infra constants for cw-s3's sweep dispatch bridge. The `GCP_SA_KEY`
// Pages secret (a dedicated SA — Batch-submit + actAs the job SA, and GCS write
// for the plan.json/STOP drops) self-signs a JWT exchanged for a cloud-platform
// access token. Workers-compatible WebCrypto only, memoized per isolate.
//
// Ported near-verbatim from gcs's `_lib/gcp.ts`, minus the multi-bucket region
// fan-out: the CoreWeave bucket is single-region, so a sweep always runs in one
// Batch region (`BATCH_REGION`).

export const GCP_PROJECT = "oa-internal-450019"
export const BATCH_REGION = "us-central1"
export const JOB_SA = `gcs-usage-job@${GCP_PROJECT}.iam.gserviceaccount.com`
// The CoreWeave-scan image (built by job/build.sh, tag `cw`); carries the marin
// CLI with `sweep manifest`/`execute` and the `.[s3]` (boto3) extras.
export const CW_IMAGE = `us-central1-docker.pkg.dev/${GCP_PROJECT}/cloud-run-source-deploy/gcs-usage-snapshot:cw`

// Storage: the shared data bucket (plan.json / manifest / logs land here, and
// it's FUSE-mounted into the Batch job at /gcs/<bucket>); the CoreWeave bucket
// + endpoint the executor deletes from.
export const DATA_BUCKET = "oa-gcs-usage-dvx"
export const CW_BUCKET = "marin-us-east-02a"
export const CW_ENDPOINT = "https://cwobject.com"

export const batchJobsUrl = (region: string = BATCH_REGION): string =>
  `https://batch.googleapis.com/v1/projects/${GCP_PROJECT}/locations/${region}/jobs`

export const secretRef = (name: string): string =>
  `projects/${GCP_PROJECT}/secrets/${name}/versions/latest`

const b64url = (buf: ArrayBuffer | Uint8Array): string => {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  let s = ""
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

async function mint(saKey: string): Promise<{ token: string; exp: number }> {
  const sa = JSON.parse(saKey) as { client_email: string; private_key: string }
  const pem = sa.private_key.replace(/-----[A-Z ]+-----|\s/g, "")
  const der = Uint8Array.from(atob(pem), c => c.charCodeAt(0))
  const key = await crypto.subtle.importKey(
    "pkcs8", der, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"],
  )
  const now = Math.floor(Date.now() / 1000)
  const enc = new TextEncoder()
  const unsigned = `${b64url(enc.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })))}.${b64url(enc.encode(JSON.stringify({
    iss: sa.client_email,
    scope: "https://www.googleapis.com/auth/cloud-platform",
    aud: "https://oauth2.googleapis.com/token",
    iat: now,
    exp: now + 3600,
  })))}`
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, enc.encode(unsigned))
  const r = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion: `${unsigned}.${b64url(sig)}`,
    }),
  })
  if (!r.ok) throw new Error(`token exchange: ${r.status} ${await r.text()}`)
  return { token: ((await r.json()) as { access_token: string }).access_token, exp: now + 3000 }
}

let memo: { k: string; token: string; exp: number } | null = null

/** Service-account JWT → OAuth access token (cloud-platform scope), memoized. */
export async function gcpToken(saKey: string): Promise<string> {
  const k = saKey.slice(-32) // key material never leaves the isolate; the memo key is a tail
  if (memo && memo.k === k && memo.exp > Date.now() / 1000) return memo.token
  const got = await mint(saKey)
  memo = { k, ...got }
  return got.token
}

/** The standard cw-sweep Batch job spec (mirrors job/cw-batch-submit.sh): the cw
 * image running `bash -c <script>`, the data bucket FUSE-mounted at /gcs/<bucket>,
 * and the CAIOS S3 creds from Secret Manager. `env` merges in per-job variables. */
export function sweepBatchSpec(script: string, env: Record<string, string> = {}): unknown {
  return {
    taskGroups: [{
      taskCount: 1,
      taskSpec: {
        runnables: [{
          container: {
            imageUri: CW_IMAGE,
            entrypoint: "/bin/bash",
            commands: ["-c", script],
            volumes: [`/mnt/disks/gcs/${DATA_BUCKET}:/gcs/${DATA_BUCKET}:rw`],
          },
        }],
        computeResource: { cpuMilli: 8000, memoryMib: 16000 },
        maxRetryCount: 0,
        maxRunDuration: "86400s",
        volumes: [{
          gcs: { remotePath: DATA_BUCKET },
          mountPath: `/mnt/disks/gcs/${DATA_BUCKET}`,
          mountOptions: ["--implicit-dirs"],
        }],
        environment: {
          variables: {
            DATA_BUCKET, CW_BUCKET, CW_ENDPOINT,
            AWS_DEFAULT_REGION: "us-east-1",
            AWS_EC2_METADATA_DISABLED: "true",
            DT_S3_ADDRESSING_STYLE: "virtual",
            ...env,
          },
          secretVariables: {
            AWS_ACCESS_KEY_ID: secretRef("cw-s3-access-key-id"),
            AWS_SECRET_ACCESS_KEY: secretRef("cw-s3-secret-access-key"),
          },
        },
      },
    }],
    allocationPolicy: {
      instances: [{ policy: { machineType: "n2-standard-8", bootDisk: { type: "pd-balanced", sizeGb: "100" } } }],
      serviceAccount: { email: JOB_SA },
      location: { allowedLocations: [`regions/${BATCH_REGION}`] },
    },
    logsPolicy: { destination: "CLOUD_LOGGING" },
  }
}

/** Submit a Batch job; returns { ok, status, text } (caller maps errors). */
export async function submitBatch(token: string, jobId: string, spec: unknown): Promise<{ ok: boolean; status: number; text: string }> {
  const r = await fetch(`${batchJobsUrl()}?job_id=${jobId}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(spec),
  })
  return { ok: r.ok, status: r.status, text: await r.text() }
}

/** Compact UTC stamp `YYYYMMDD-HHMMSS` for a job id. */
export const jobStamp = (): string =>
  new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15).toLowerCase()

/** The FUSE-mount path of a run dir inside the Batch job. */
export const runMountPath = (jobId: string): string => `/gcs/${DATA_BUCKET}/sweep/cw/runs/${jobId}`
export const runGsPath = (jobId: string): string => `gs://${DATA_BUCKET}/sweep/cw/runs/${jobId}`
