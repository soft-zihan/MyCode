import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.tsx'
import { installAuthFetch } from './api/auth.ts'
import './index.css'

installAuthFetch()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
