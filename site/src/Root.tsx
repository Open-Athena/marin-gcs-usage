import { Route, Routes } from 'react-router-dom'
import App from './App'
import { AuthGate } from './AuthGate'
import { FilesPage } from './FilesPage'
import { OgPage } from './OgPage'

// `/files/*` → scan browser; `/og` → redacted fixed-size treemap for the
// og:image screenshot (public, ungated — it's what unfurl crawlers render);
// everything else → the treemap app. The two data-backed routes sit behind
// <AuthGate>, which shows a login wall when there's no CF Access session.
export default function Root() {
  return (
    <Routes>
      <Route path="/og" element={<OgPage />} />
      <Route path="/files/*" element={<AuthGate><FilesPage /></AuthGate>} />
      <Route path="*" element={<AuthGate><App /></AuthGate>} />
    </Routes>
  )
}
