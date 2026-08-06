import { Route, Routes } from 'react-router-dom'
import App from './App'
import { FilesPage } from './FilesPage'

// `/files/*` → scan browser; everything else → the treemap app (unchanged;
// its `use-prms` search-param state is orthogonal to path routing).
export default function Root() {
  return (
    <Routes>
      <Route path="/files/*" element={<FilesPage />} />
      <Route path="*" element={<App />} />
    </Routes>
  )
}
