import { Link } from 'react-router-dom'
import { FileTree } from '@rdub/file-tree/react'
import { HttpStore } from '@rdub/file-tree/stores/http'
import { ParquetViewer } from '@rdub/file-tree/renderers/parquet'
import { useIdent } from './auth'

// Same-origin proxy (CF Pages Function, app session required) → the raw scan
// bucket. `prefixes` in the function allow-lists listing/ + snapshots/.
const store = HttpStore('/v1/files')
const BUCKET_URI = 'gs://oa-gcs-usage-dvx'

export function FilesPage() {
  const ident = useIdent()
  return (
    <div style={{ padding: '1rem', maxWidth: 1100, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <Link to="/" style={{ fontSize: '0.9em' }}>← treemap</Link>
        <span style={{ fontSize: '0.85em', color: 'var(--ink-3, #888)' }}>
          <code>{BUCKET_URI}</code> · access-gated{ident ? <> · signed in as <b>{ident.email}</b></> : null}
        </span>
      </div>
      <FileTree
        store={store}
        routeBase="/files"
        title="Scan data — raw listings + snapshots"
        parquetRenderer={ParquetViewer}
      />
    </div>
  )
}
