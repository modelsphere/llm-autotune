import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath, URL } from 'node:url'
import { constants, gzipSync } from 'node:zlib'

import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { defineConfig, type Plugin } from 'vite'

/** A `.gz` next to every text file of the build, so nginx (`gzip_static`)
 *  sends it as it is, at the strongest level, instead of compressing it again
 *  on every request. */
function precompress(): Plugin {
  let outDir = 'dist'
  return {
    name: 'precompress',
    apply: 'build',
    configResolved(config) {
      outDir = config.build.outDir
    },
    closeBundle() {
      const walk = (dir: string) => {
        for (const name of readdirSync(dir)) {
          const path = join(dir, name)
          if (statSync(path).isDirectory()) walk(path)
          else if (/\.(js|css|html|svg|json)$/.test(name) && statSync(path).size > 1024) {
            writeFileSync(`${path}.gz`,
              gzipSync(readFileSync(path), { level: constants.Z_BEST_COMPRESSION }))
          }
        }
      }
      walk(outDir)
    },
  }
}

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus component by component, in the pages that use them, rather
    // than all of it in the first download. Its stylesheet stays whole
    // (main.ts): small once compressed, and nothing ever renders unstyled.
    Components({
      resolvers: [ElementPlusResolver({ importStyle: false })],
      dts: false,
    }),
    precompress(),
  ],
  // `@/` is src/, so a plugin folder (src/plugins/installed/<name>) imports
  // `@/plugins/api` wherever it was copied from.
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: {
    rollupOptions: {
      output: {
        // Libraries in chunks of their own: they change far less often than
        // the app, so after a redeploy the browser still has them cached.
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (/node_modules\/(echarts|zrender)\//.test(id)) return 'echarts'
          if (/node_modules\/(highlight\.js|markdown-it|mdurl|uc\.micro|linkify-it|punycode\.js|entities)\//.test(id)) {
            return 'markdown'
          }
          if (/node_modules\/(vue|@vue|vue-router|pinia|axios)\//.test(id)) return 'vue'
          return undefined
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // Overridable because "localhost" means the vite process's host: on a
      // laptop that is the dev API, inside the dev compose container it must
      // be the api service (deploy/docker-compose.dev.yml sets it). `/api/`
      // with its slash, as nginx has it: a bare `/api` prefix also sent the
      // /api-keys page to the API.
      '/api/': {
        target: process.env.VITE_API_PROXY || 'http://localhost:28100',
        changeOrigin: true,
      },
    },
  },
})
