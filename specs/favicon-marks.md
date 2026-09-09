# Spec: favicon marks (GCS main + CoreWeave-S3 branch)

From the disk-tree session — mgu is a deployment of disk-tree, so it inherits disk-tree's **mark grammar**: a rounded-square treemap that reads as a Mondrian (the app's own 2+4 rect partition, same-color rules), with cells repainted per deployment and — for Marin deployments — a **cream sail** rising from the map.

Design board (renders each mark at 48/32/16 + tab mock): https://claude.ai/code/artifact/6c2dabbb-0908-46fa-b3eb-7834f5d02cba

## Files (ready to use)

Two finished SVGs sit in `tmp/logo-wip/` (untracked — copy into place, don't rely on them staying there):

- `tmp/logo-wip/favicon-gcs.svg` — **main branch** (GCS)
- `tmp/logo-wip/favicon-cw-s3.svg` — **cw-s3 branch** (CoreWeave S3)

Both build on the earlier `tmp/logo-wip/favicon.svg`, shifted off the cold navy ground onto OA warm ink.

## Palettes

Shared: ground + rules `#1f1e1b` (OA warm ink), Marin sail `#e7ddc7`.

- **GCS (main):** OA deep-blue `#224a82` · navy `#3a4656` · terracotta `#9c4f2f` · teal `#345a51` · **Stanford cardinal** `#8c1515` · ochre `#c0883a`. The blue→red→teal→gold spread nods to Google's four-color mark without copying it; cardinal ties to Stanford; cream sail to Marin.
- **cw-s3 (branch):** **green-dominant** — S3 bucket greens `#3f8624`/`#2b6a1c` + CoreWeave black `#0a0a0a` + OA bronze `#9e6d43` · ochre `#c0883a` · steel `#465c6e`. Green-dominant so it reads as unmistakably *not* the GCS mark in a 16px tab strip.

## Wiring (per branch)

1. `cp tmp/logo-wip/favicon-gcs.svg ui/public/favicon.svg` on `main`; `favicon-cw-s3.svg` on `cw-s3`.
2. Confirm `ui/index.html` references `<link rel="icon" type="image/svg+xml" href="/favicon.svg" />` (it does).
3. Remove the leftover scaffold `ui/public/vite.svg` if still present.
4. Regenerate the `og.jpg`/`og.png` social image if it embeds the old mark (the OG image is otherwise an app screenshot — leave it if it doesn't show the favicon).

## Rationale (naming)

disk-tree's own base mark is **Slate** (rust/steel/ochre/olive on the same warm ink) — the two mgu marks are recognizably the same family, retinted for GCS vs. CoreWeave. Nothing was renamed; "Slate/Index/Delta" were mark-style labels, not product names.
