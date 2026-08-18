import { Route, Routes } from 'react-router-dom'
import App from './App'
import { AuthGate } from './AuthGate'
import { FilesPage } from './FilesPage'
import { OgPage } from './OgPage'
import { STORES } from './stores'

// `/files/*` → scan browser; `<store>/og` → redacted fixed-size treemap for that
// store's og:image screenshot (public, ungated — it's what unfurl crawlers
// render); every other path → the treemap app, which picks its store from the
// path (`/` = GCS, `/cw` = CoreWeave). The two data-backed routes sit behind
// <AuthGate>, which shows a login wall when there's no CF Access session.
export default function Root() {
  return (
    <Routes>
      {STORES.map(s => (
        <Route key={s.key} path={`${s.path.replace(/\/$/, '')}/og`} element={<OgPage store={s} />} />
      ))}
      <Route path="/files/*" element={<AuthGate><FilesPage /></AuthGate>} />
      <Route path="*" element={<AuthGate><App /></AuthGate>} />
    </Routes>
  )
}
