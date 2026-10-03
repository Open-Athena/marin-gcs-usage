// cw-s3.oa.dev's store (oa-decoupling step 6).
import type { Store } from '../stores'

const store: Store = {
  key: 'cw',
  label: 'CoreWeave',
  title: 'Marin CoreWeave usage',
  desc: 'Storage usage of the Marin CoreWeave buckets — treemap, diffs over time, and reviewed deletions.',
  path: '/',
  scheme: 's3://',
  base: '/data/cw',
  ogImage: '/og.jpg',
  prices: false,
  staging: true,   // the table's trash gesture + /staged (needs `stage_batches`, migrations/cw/0002)
  owners: false,
  executor: 'plan-sweep',
  lifecycle: { tracked: 'job/cw-lifecycle.json', rules: 's3', recordedFrom: '2026-09-17' },
  buckets: ['marin-us-east-02a', 'hero-checkpoints'],
  peer: { label: 'GCS usage', href: 'https://gcs.oa.dev/' },
  rootLabel: 'all buckets',
  objectsNote: 'CoreWeave objects are written once by the training jobs and never rewritten in place, so created is the object’s only time.',
  wall: {
    restrict: 'Access is limited to Open Athena and CoreWeave staff.',
    signIn: 'Sign in with Open Athena',
    how: 'Use Google with your Open Athena or CoreWeave account, or have a one-time code emailed to it (no Google account needed). Invited guests can also use a personal share link.',
  },
}

export default store
