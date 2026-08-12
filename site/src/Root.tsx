import { Route, Routes } from 'react-router-dom'
import App from './App'
import { FilesPage } from './FilesPage'
import { OgPage } from './OgPage'

// `/files/*` → scan browser; `/og` → redacted fixed-size treemap for the
// og:image screenshot; everything else → the treemap app (unchanged; its
// `use-prms` search-param state is orthogonal to path routing).
export default function Root() {
  return (
    <Routes>
      <Route path="/files/*" element={<FilesPage />} />
      <Route path="/og" element={<OgPage />} />
      <Route path="*" element={<App />} />
    </Routes>
  )
}
