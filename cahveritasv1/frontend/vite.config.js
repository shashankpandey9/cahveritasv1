import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: UI on 5173, FastAPI on 8000 (proxied). Build output is served by
// FastAPI itself in single-origin deploys (the Databricks App path).
export default defineConfig({
  plugins: [react()],
  logLevel: 'error',
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } }
  },
  build: { outDir: 'dist' }
})
