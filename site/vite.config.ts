import { existsSync, readFileSync } from 'node:fs'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Ports: `devPort` in package.json (vite), `PORT` env overrides (a second gcs
// dev stack, or when another worktree already holds 3253); wrangler pages dev
// (the Functions) is always the next port up — `./dev` derives it the same way.
const PORT = Number(process.env.PORT ?? JSON.parse(readFileSync('package.json', 'utf8')).devPort)
const WRANGLER = `http://localhost:${PORT + 1}`

// dev only: serve a locally-generated `tmp/series.json` (from `dt-cloud series
// -r http://localhost:3254/data -o tmp/series.json`) at /data/series.json, so
// the scoped size chart can be previewed before the index is published to the
// bucket. Registered in the plugin body so it pre-empts the /data proxy; a no-op
// (falls through to the bucket) when the file is absent.
const devSeriesIndex = {
  name: 'dev-series-index',
  configureServer(server: { middlewares: { use: (path: string, fn: (req: unknown, res: { setHeader: (k: string, v: string) => void; end: (b: Buffer) => void }, next: () => void) => void) => void } }) {
    server.middlewares.use('/data/series.json', (_req, res, next) => {
      const p = 'tmp/series.json'
      if (existsSync(p)) { res.setHeader('content-type', 'application/json'); res.end(readFileSync(p)) }
      else next()
    })
  },
}

export default defineConfig({
  plugins: [react(), devSeriesIndex],
  server: {
    port: PORT,
    host: true,
    // Trust hosts unconditionally — personal dev server on a trusted tailnet.
    // (`VITE_ALLOWED_HOSTS=.rbw.sh` can't cover the bare MagicDNS name `m3`.)
    allowedHosts: true,
    // dev only: forward the Pages Functions (snapshot data + scan-browser API)
    // to the local `wrangler pages dev` (run it on :3254 with GCS HMAC creds in
    // .dev.vars). Both /data and /v1/files now read live from the bucket.
    proxy: {
      '/data': WRANGLER,
      '/v1/files': WRANGLER,
      '/api': WRANGLER,
      // Sign-in Functions (`/auth/google*`, `/auth/email/*`). Keep
      // the browser's Host header (Vite's string-target default rewrites it to
      // the wrangler port): the OIDC callback + emailed links derive their origin
      // from it, so they resolve to `http://localhost:<PORT>/…` — the URI that
      // must be registered on the Google client for local sign-in to work.
      '/auth': { target: WRANGLER, changeOrigin: false },
    },
  },
  // The workspace-linked `@rdub/file-tree` calls `useLocation` etc. — force a
  // single instance of these so its hooks share the app's Router/React context
  // (else the rollup build bundles a 2nd copy → "useLocation outside <Router>").
  resolve: {
    dedupe: ['react', 'react-dom', 'react-router-dom'],
  },
  optimizeDeps: {
    exclude: ['@disk-tree/react'],
  },
})
