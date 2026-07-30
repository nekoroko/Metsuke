import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 開発時は Vite dev server（5173）から FastAPI（8000）へプロキシする。
// 本番は vite build の成果物を FastAPI の StaticFiles が配るので同一オリジン。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        configure: (proxy) => {
          // SSE を途中でバッファさせない
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache, no-transform'
            }
          })
        },
      },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
