// adapted from shannon-prime-kairos/ui/src/main.jsx
import React from 'react'
import { createRoot } from 'react-dom/client'
import './kit/fonts.js'
import './kit/tokens.css'
import './kit/kit.css'
import './shell.css'
import Desktop from './Desktop.jsx'

createRoot(document.getElementById('root')).render(<React.StrictMode><Desktop /></React.StrictMode>)
