import { Link } from 'react-router-dom'
import { FileTree } from '@rdub/file-tree/react'
import { HttpStore } from '@rdub/file-tree/stores/http'
import { makeParquetViewer } from '@rdub/file-tree/renderers/parquet'
import type { ElideCtx } from '@rdub/file-tree/renderers/table'
import { Tooltip } from './Tooltip'

// Same-origin proxy (CF Pages Function, behind CF Access) → the raw scan bucket.
const store = HttpStore('/v1/files')

// The parquet viewer with file-tree's `elide` strategy for wide cells (scan
// listings' `name`/`key` columns are long object paths): the column clips so the
// table stays scannable, and the clipped tail comes back on hover as a floating
// tooltip — our @floating-ui `Tooltip`, wrapping the cell's own node — rather
// than the browser's slow native `title`. file-tree ships no tooltip dependency;
// the consumer supplies the panel. Columns are also draggable (double-click a
// handle to auto-fit) and remembered per column *set* (`schema` scope): every
// scan's parquet shares one schema, so a width pinned on one date carries over.
const elideTooltip = (ctx: ElideCtx) =>
  ctx.text == null ? ctx.node : <Tooltip content={<code className="elide-full">{ctx.text}</code>}>{ctx.node}</Tooltip>
const parquetViewer = makeParquetViewer({ elide: { tooltip: elideTooltip }, resizableColumns: { scope: 'schema' } })

export function FilesPage() {
  return (
    <div style={{ padding: '1rem', maxWidth: 1100, margin: '0 auto' }}>
      <Link to="/" style={{ fontSize: '0.9em' }}>← treemap</Link>
      <FileTree
        store={store}
        routeBase="/files"
        title="Scan data — raw listings + snapshots"
        parquetRenderer={parquetViewer}
      />
    </div>
  )
}
