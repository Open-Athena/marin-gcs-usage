// The `/meta` secondary store (cw-s3's STORES_EXTRA; oa-decoupling step 6).
import type { Store } from '../stores'

const store: Store = {
  // The deploy's own storage (specs/multi-store.md): the scan + index data
  // the deployments write — the GCS snapshot bucket and its R2 mirror — as
  // a secondary store, mounted under `/meta` beside a primary (`STORES_EXTRA`).
  // Its data is the same layer-3 shape under `snapshots/meta/`, served by the
  // Functions with `store=meta` (`STORES_JSON` → `SNAPSHOTS_SUBDIR = meta`).
  // Staff-only server-side (the store's `scope`); no ownership ledger, no
  // trash gesture, no storage-class prices.
  key: 'meta',
  label: 'Meta',
  title: 'Our storage — scan & index data',
  desc: 'The scan and index data these deployments write (the GCS snapshot bucket and its R2 mirror) — treemap, sizes over time, and diffs.',
  path: '/meta',
  scheme: 'gs://',
  base: '/data/meta',
  ogImage: '/og.jpg',
  prices: false,
  staging: false,
  owners: false,
  executor: 'plan-sweep',
  buckets: ['oa-gcs-usage-dvx', 'oa-cw-s3-usage-index'],
  rootLabel: 'our storage',
  objectsNote: 'Scan outputs are written once per job run and never rewritten in place, so created is the object’s publish time.',
  wall: {
    restrict: 'The meta store is limited to Open Athena staff.',
    signIn: 'Sign in with Open Athena',
  },
}

export default store
