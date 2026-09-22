import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      // Overridable because "localhost" means the vite process's host: on a
      // laptop that is the dev API, inside the dev compose container it must
      // be the api service (deploy/docker-compose.dev.yml sets it).
      '/api': {
        target: process.env.VITE_API_PROXY || 'http://localhost:28100',
        changeOrigin: true,
      },
    },
  },
})
