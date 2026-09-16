import { Route, Routes } from 'react-router-dom'
import { HotkeysProvider } from 'use-kbd'
import App from './App'
import { AuthGate } from './AuthGate'
import { FilesPage } from './FilesPage'
import { OgPage } from './OgPage'
import { SweepPage } from './SweepPage'
import { STORES } from './stores'

// `/files/*` → scan browser; `<store>/og` → redacted fixed-size treemap for that
// store's og:image screenshot (public, ungated — it's what unfurl crawlers
// render); every other path → the treemap app, which picks its store from the
// path. The two data-backed routes sit behind
// <AuthGate>, which shows a login wall when there's no CF Access session.
// One hotkey/omnibar registry for the whole site (pages register their own
// actions on top of the shared ones).
export default function Root() {
  return (
    <HotkeysProvider config={{ storageKey: 'gcs-usage' }}>
    <Routes>
      {STORES.map(s => (
        <Route key={s.key} path={`${s.path.replace(/\/$/, '')}/og`} element={<OgPage store={s} />} />
      ))}
      <Route path="/files/*" element={<AuthGate><FilesPage /></AuthGate>} />
      <Route path="/sweep" element={<AuthGate><SweepPage /></AuthGate>} />
      <Route path="*" element={<AuthGate><App /></AuthGate>} />
    </Routes>
    </HotkeysProvider>
  )
}
