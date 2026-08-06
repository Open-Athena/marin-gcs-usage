import { Link } from 'react-router-dom'
import { FileTree } from '@rdub/file-tree/react'
import { HttpStore } from '@rdub/file-tree/stores/http'
import { ParquetViewer } from '@rdub/file-tree/renderers/parquet'

// Same-origin proxy (CF Pages Function, behind CF Access) → the raw scan bucket.
const store = HttpStore('/v1/files')

export function FilesPage() {
  return (
    <div style={{ padding: '1rem', maxWidth: 1100, margin: '0 auto' }}>
      <Link to="/" style={{ fontSize: '0.9em' }}>← treemap</Link>
      <FileTree
        store={store}
        routeBase="/files"
        title="Scan data — raw listings + snapshots"
        parquetRenderer={ParquetViewer}
      />
    </div>
  )
}
