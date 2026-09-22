/** Entry point of the self-contained HTML export: `AutotuneReport.mountStandalone`
 *  reads the JSON the backend inlined (every language of the report, its frozen
 *  comparison, assets as data URIs) and renders it, with a language switch
 *  when there is more than one. Built by `npm run build:report` into
 *  backend/app/agent/static/report-renderer.js. */

import type { Comparison } from './data'
import { renderReport } from './render'
import type { Lang } from './strings'

interface Payload {
  initial: string
  reports: {
    id: number
    lang: string
    title: string
    markdown: string
    comparison: Comparison
    labels: string[]
    assets: Record<string, string>
  }[]
}

const LANG_NAMES: Record<string, string> = { en: 'English', zh: '中文' }

export { renderReport }

export function mountStandalone(rootId: string, dataId: string): void {
  const root = document.getElementById(rootId)
  const data = document.getElementById(dataId)
  if (!root || !data) return
  const payload: Payload = JSON.parse(data.textContent ?? '{}')
  const reports = payload.reports ?? []
  if (!reports.length) return

  root.classList.add('ar-page')
  const bar = document.createElement('div')
  bar.className = 'ar-langs'
  const body = document.createElement('div')
  root.append(bar, body)

  let dispose: (() => void) | null = null
  const show = (lang: string) => {
    const report = reports.find((r) => r.lang === lang) ?? reports[0]
    dispose?.()
    dispose = renderReport(body, {
      markdown: report.markdown,
      comparison: report.comparison,
      lang: (report.lang === 'zh' ? 'zh' : 'en') as Lang,
      labels: report.labels,
      resolveAsset: (name) => report.assets[name] ?? name,
    })
    document.title = report.title
    document.documentElement.lang = report.lang === 'zh' ? 'zh-CN' : 'en'
    bar.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.lang === report.lang))
    try {
      history.replaceState(null, '', `#${report.lang}`)
    } catch {
      /* file:// in some browsers */
    }
  }
  if (reports.length > 1) {
    for (const r of reports) {
      const b = document.createElement('button')
      b.type = 'button'
      b.dataset.lang = r.lang
      b.textContent = LANG_NAMES[r.lang] ?? r.lang
      b.addEventListener('click', () => show(r.lang))
      bar.append(b)
    }
  }
  const fromHash = location.hash.replace('#', '')
  show(reports.some((r) => r.lang === fromHash) ? fromHash : payload.initial)
}
