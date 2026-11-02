// adapted from shannon-prime-kairos/ui/vite.config.js
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const PRIMER = 'http://127.0.0.1:8810'

/* serve.py only answers Host 127.0.0.1:8810 / localhost:8810 and refuses requests from a
 * foreign Origin. The dev proxy rewrites Host (changeOrigin), and rewrites Origin to Primer's
 * own only when it is this dev server's own origin; any other Origin (another localhost app
 * calling :5173) passes through untouched, so Primer refuses it. X-Primer passes through. */
export function proxyOrigin(origin, devHost, target) {
  return origin && devHost && origin.toLowerCase() === `http://${devHost}`.toLowerCase() ? target : origin
}

// Builds to ui/dist, which serve.py serves at /. Node is a build-time dependency only.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: Object.fromEntries(['/v1', '/health'].map(p => [p, {
      target: PRIMER,
      changeOrigin: true,
      configure: (proxy, options) => proxy.on('proxyReq', (proxyReq, req) => {
        const origin = req.headers.origin
        if (origin) proxyReq.setHeader('origin', proxyOrigin(origin, req.headers.host, String(options.target)))
      }),
    }])),
  },
  test: { environment: 'node' },
})
