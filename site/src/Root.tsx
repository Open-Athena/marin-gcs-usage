import { Navigate, Route, Routes } from 'react-router-dom'
import { HotkeysProvider } from 'use-kbd'
import { AdminDbPage } from './AdminDbPage'
import { AdminPage } from './AdminPage'
import App from './App'
import { AuthGate } from './AuthGate'
import { FilesPage } from './FilesPage'
import { MarksPage } from './MarksPage'
import { SweepPage } from './SweepPage'
import { OgPage } from './OgPage'
import { UserOgPage, UserPage, UsersOgPage, UsersPage } from './UserPage'
import { STORES } from './stores'

// `/files/*` → scan browser; `<store>/og` → redacted fixed-size treemap for that
// store's og:image screenshot (public, ungated — it's what unfurl crawlers
// render); every other path → the treemap app, which picks its store from the
// path. The two data-backed routes sit behind
// <AuthGate>, which shows a login wall when there's no CF Access session.
// One hotkey/omnibar registry for the whole site (SiteKbd renders the chrome
// on each page; pages register their own actions on top of the shared ones).
export default function Root() {
  return (
    <HotkeysProvider config={{ storageKey: 'gcs-usage' }}>
    <Routes>
      {STORES.map(s => (
        <Route key={s.key} path={`${s.path.replace(/\/$/, '')}/og`} element={<OgPage store={s} />} />
      ))}
      <Route path="/admin" element={<AuthGate><AdminPage /></AuthGate>} />
      <Route path="/admin/db" element={<AuthGate><AdminDbPage /></AuthGate>} />
      <Route path="/admin/db/:table" element={<AuthGate><AdminDbPage /></AuthGate>} />
      <Route path="/files/*" element={<AuthGate><FilesPage /></AuthGate>} />
      <Route path="/marks" element={<AuthGate><MarksPage /></AuthGate>} />
      <Route path="/sweep" element={<AuthGate><SweepPage /></AuthGate>} />
      <Route path="/users/og" element={<UsersOgPage />} />
      <Route path="/user/:id/og" element={<UserOgPage />} />
      <Route path="/users" element={<AuthGate><UsersPage /></AuthGate>} />
      <Route path="/user/:id" element={<AuthGate><UserPage /></AuthGate>} />
      {/* The review lenses fold onto `/` now (LensBar) — /mark is just the map. */}
      <Route path="/mark" element={<Navigate to="/" replace />} />
      <Route path="*" element={<AuthGate><App /></AuthGate>} />
    </Routes>
    </HotkeysProvider>
  )
}
