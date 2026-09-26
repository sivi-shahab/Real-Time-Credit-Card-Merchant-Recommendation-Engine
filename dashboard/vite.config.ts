/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/admin': { target: 'http://localhost:8000', changeOrigin: true },
      '/bff': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: {
    // ponytail: echarts (core + bar/grid/tooltip, already imported piecemeal) is one ~500 kB
    // chunk that only chart pages load and that cannot split further. The limit sits just
    // above it so any other chunk that grows still warns; lazy-mount charts if it matters.
    chunkSizeWarningLimit: 560,
  },
  test: { environment: 'jsdom', globals: true },
})
