import { cloneElement, isValidElement, type ReactElement } from 'react'
import { Link } from 'react-router-dom'
import { FileTree } from '@rdub/file-tree/react'
import { HttpStore } from '@rdub/file-tree/stores/http'
import { makeParquetViewer, type ParquetCellRenderer } from '@rdub/file-tree/renderers/parquet'
import type { ElideCtx } from '@rdub/file-tree/renderers/table'
import { renderJsonTree } from '@rdub/file-tree/renderers/json'
import { CsvViewer } from '@rdub/file-tree/renderers/csv'
import { renderMarkdown } from '@rdub/file-tree/renderers/markdown'
import { renderCode } from '@rdub/file-tree/renderers/code'
import NotebookViewer from '@rdub/file-tree/renderers/notebook'
import { useUrlPersistedState } from '@rdub/file-tree/url-state'
import { Tooltip } from './Tooltip'
import { CopyName } from './CopyName'
import { fmtBytes, fmtN } from './types'

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
// Only values long enough to plausibly clip get the hover panel; short scalars
// (sizes, timestamps, ids after `renderCell` below) would just grow a
// tooltip that repeats the cell. file-tree can't yet tell us whether a cell
// actually overflowed (`onlyWhenClipped` is its documented next axis), so
// this is a length heuristic in the meantime.
const ELIDE_MIN = 40
const elideTooltip = (ctx: ElideCtx) =>
  ctx.text == null || ctx.text.length < ELIDE_MIN ? ctx.node : <Tooltip content={<code className="elide-full">{ctx.text}</code>}>{ctx.node}</Tooltip>

const SIZE_COLS = /^(size|size_bytes|bytes|b)$/
const HEX_ID_COLS = /(^|_)(id|version_id|etag|md5|sha\w*|hash)$/
const HEX_SHOW = 8
// Per-cell formatting on top of file-tree's defaults: byte columns read as
// TiB/GiB with the exact count on hover; long hex ids show a prefix with the
// full value (click-to-copy) on hover; and file-tree's default temporal cell
// carries a native `title` (the raw epoch) that we drop — the browser tooltip
// is slow, and the formatted timestamp is the value anyone wants.
const renderCell: ParquetCellRenderer = ({ value, column, defaultNode }) => {
  const name = column.name
  if (SIZE_COLS.test(name) && (typeof value === 'number' || typeof value === 'bigint')) {
    const n = Number(value)
    // `fmtBytes` floors at Ki (a 340-byte object would read `0 Ki`); below that, bytes verbatim
    return <Tooltip content={<code>{fmtN(n)} B</code>}><span style={{ fontVariantNumeric: 'tabular-nums' }}>{n < 1024 ? `${n} B` : fmtBytes(n)}</span></Tooltip>
  }
  if (HEX_ID_COLS.test(name) && typeof value === 'string' && value.length > HEX_SHOW + 4) {
    return <CopyName text={value}><code>{value.slice(0, HEX_SHOW)}…</code></CopyName>
  }
  return isValidElement(defaultNode) && (defaultNode.props as { title?: string }).title != null
    ? cloneElement(defaultNode as ReactElement<{ title?: string }>, { title: undefined })
    : defaultNode
}
const parquetViewer = makeParquetViewer({ elide: { tooltip: elideTooltip }, renderCell, resizableColumns: { scope: 'schema' } })

export function FilesPage() {
  return (
    <div style={{ padding: '1rem', maxWidth: 1100, margin: '0 auto' }}>
      <Link to="/" style={{ fontSize: '0.9em' }}>← treemap</Link>
      <FileTree
        store={store}
        routeBase="/files"
        title="Scan data — raw listings + snapshots"
        parquetRenderer={parquetViewer}
        // Rich viewers instead of plaintext `<pre>`: JSON/YAML as a collapsible
        // tree with search + jq (`?jq=`), CSV/TSV as a paginated table, Markdown
        // + notebooks rendered, code syntax-highlighted. `useUrlPersistedState`
        // threads their search / pagination / jq state into the URL.
        jsonRenderer={renderJsonTree}
        csvRenderer={CsvViewer}
        markdownRenderer={renderMarkdown}
        codeRenderer={renderCode}
        notebookRenderer={NotebookViewer}
        usePersistedState={useUrlPersistedState}
      />
    </div>
  )
}
