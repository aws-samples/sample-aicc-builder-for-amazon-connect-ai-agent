import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  define: {
    // Fix for amazon-cognito-identity-js which requires Node.js globals
    global: 'globalThis',
  },
  server: {
    port: 3000,
    open: true,
    // Local dev: proxy backend routes to the FastAPI server started by
    // backend/ecs/local-dev.sh (port 8080). Lets the same-origin
    // ws://localhost:3000/ws + /api calls reach the backend without CORS or
    // mixed-content issues. No effect on production builds.
    proxy: {
      '/ws': { target: 'ws://localhost:8080', ws: true },
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/invocations': { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
