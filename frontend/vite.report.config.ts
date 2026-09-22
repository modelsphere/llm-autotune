import { defineConfig } from 'vite'

/** The report renderer as one self-executing script, for the backend's
 *  self-contained HTML export (app/agent/export.py inlines it). CSS rides
 *  inside the JS (`?inline`), so the file is the whole renderer.
 *  Rebuild with `npm run build:report` whenever src/report/ changes, and
 *  commit the output. */
export default defineConfig({
  build: {
    lib: {
      entry: 'src/report/standalone.ts',
      name: 'AutotuneReport',
      formats: ['iife'],
      fileName: () => 'report-renderer.js',
    },
    outDir: '../backend/app/agent/static',
    emptyOutDir: false,
    minify: true,
    sourcemap: false,
  },
  define: { 'process.env.NODE_ENV': '"production"' },
})
