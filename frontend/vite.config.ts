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
    // Local dev: proxy backend routes to a backend. Defaults to the local
    // FastAPI server (backend/ecs/local-dev.sh, port 8080). Set
    // VITE_DEV_BACKEND to a deployed ALB host to run the local frontend against
    // the REAL backend, e.g.
    //   VITE_DEV_BACKEND=aiccbuilderecs-dev-alb-...elb.amazonaws.com npm run dev
    // Lets same-origin ws://localhost:3000/ws + /api calls reach the backend
    // without CORS or mixed-content issues. No effect on production builds.
    proxy: (() => {
      const backend = process.env.VITE_DEV_BACKEND;
      const httpTarget = backend ? `http://${backend}` : 'http://localhost:8080';
      const wsTarget = backend ? `ws://${backend}` : 'ws://localhost:8080';
      return {
        '/ws': { target: wsTarget, ws: true, changeOrigin: true },
        '/api': { target: httpTarget, changeOrigin: true },
        '/invocations': { target: httpTarget, changeOrigin: true },
      };
    })(),
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
