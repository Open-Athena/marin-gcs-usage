import { FileTree } from '@rdub/file-tree/react'
import { HttpStore } from '@rdub/file-tree/stores/http'
import { ParquetViewer } from '@rdub/file-tree/renderers/parquet'
import { SiteNav } from './SiteNav'

// Same-origin proxy (CF Pages Function, app session required) → the raw scan
// bucket. `prefixes` in the function allow-lists listing/ + snapshots/.
const store = HttpStore('/v1/files')
const BUCKET_URI = 'gs://oa-gcs-usage-dvx'

export function FilesPage() {
  return (
    <main className="files-page" style={{ padding: '1rem', maxWidth: 1100, margin: '0 auto' }}>
      <SiteNav />
      <p className="sub" style={{ margin: '0 0 0.6em' }}>
        Raw scan store — <code>{BUCKET_URI}</code> (<code>listing/</code> + <code>snapshots/</code>), access-gated.
      </p>
      <FileTree
        store={store}
        routeBase="/files"
        title="Scan data — raw listings + snapshots"
        parquetRenderer={ParquetViewer}
      />
    </main>
  )
}
