import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import Root from './Root'
import { UnitsProvider } from './units'
import 'use-kbd/styles.css'
import './app.scss'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <UnitsProvider>
        <Root />
      </UnitsProvider>
    </BrowserRouter>
  </StrictMode>,
)
