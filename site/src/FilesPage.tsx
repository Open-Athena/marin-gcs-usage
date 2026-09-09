import { FileTree } from '@rdub/file-tree/react'
import { HttpStore } from '@rdub/file-tree/stores/http'
import { makeParquetViewer } from '@rdub/file-tree/renderers/parquet'
import type { ElideCtx } from '@rdub/file-tree/renderers/table'
import { useLocation } from 'react-router-dom'
import { SiteNav } from './SiteNav'
import { SiteKbd } from './SiteKbd'
import { useDocTitle } from './title'
import { Tooltip } from './Tooltip'

// Same-origin proxy (CF Pages Function, app session required) → the raw scan
// bucket. `prefixes` in the function allow-lists listing/ + snapshots/.
const store = HttpStore('/v1/files')
const BUCKET_URI = 'gs://oa-gcs-usage-dvx'

// The parquet viewer with file-tree's `elide` strategy for wide cells (the
// sweep logs' `name`/`dir` columns are long GCS paths): the column still clips
// at 30em so the table stays scannable, and the clipped tail comes back on
// hover as a rich floating tooltip — our floating-ui `Tooltip`, wrapping the
// cell's own node — rather than the browser's slow native `title`. file-tree
// ships no tooltip dependency; the consumer supplies the panel (spec:
// file-tree `specs/table-wide-columns-and-tooltips.md`, feature #4).
const elideTooltip = (ctx: ElideCtx) =>
  ctx.text == null ? ctx.node : <Tooltip content={<code className="elide-full">{ctx.text}</code>}>{ctx.node}</Tooltip>
// Column widths are draggable (double-click a handle to auto-fit) and remembered
// per column *set*: every scan's `path-index.parquet` shares one schema, so a
// width pinned on one date carries to the others.
const parquetViewer = makeParquetViewer({ elide: { tooltip: elideTooltip }, resizableColumns: { scope: 'schema' } })

export function FilesPage() {
  // Reflect where in the store the reader is drilled: `.../sweep/runs` →
  // "runs · Files", a parquet file → "<file> · Files", the root → "Files".
  const { pathname } = useLocation()
  const seg = decodeURIComponent(pathname.replace(/^\/files\/?/, '').replace(/\/$/, '').split('/').pop() ?? '')
  useDocTitle(seg || undefined, 'Files')
  return (
    <main className="files-page" style={{ padding: '1rem', '--pad-t': '1rem', '--pad-x': '1rem', maxWidth: 1100, margin: '0 auto' } as React.CSSProperties}>
      <SiteNav />
      <p className="sub" style={{ margin: '0 0 0.6em' }}>
        Raw scan store — <code>{BUCKET_URI}</code> (<code>listing/</code> + <code>snapshots/</code>), access-gated.
      </p>
      <FileTree
        store={store}
        routeBase="/files"
        title="Scan data — raw listings + snapshots"
        parquetRenderer={parquetViewer}
      />
      <SiteKbd />
    </main>
  )
}
