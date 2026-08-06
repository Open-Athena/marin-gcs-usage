import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const allowedHosts = process.env.VITE_ALLOWED_HOSTS?.split(',') ?? []

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3253,
    host: true,
    allowedHosts,
    // dev only: forward the scan-browser API to the local wrangler Pages Function
    proxy: { '/v1/files': 'http://localhost:3254' },
  },
  optimizeDeps: {
    exclude: ['@rdub/file-tree'],
  },
  // The workspace-linked `@rdub/file-tree` calls `useLocation` etc. — force a
  // single instance of these so its hooks share the app's Router/React context
  // (else the rollup build bundles a 2nd copy → "useLocation outside <Router>").
  resolve: {
    dedupe: ['react', 'react-dom', 'react-router-dom'],
  },
})
