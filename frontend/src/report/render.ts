/** Render a report: the agent's markdown, with its chart/table/command blocks
 *  drawn from the frozen comparison.
 *
 *  One function for two homes — the platform's report page and the
 *  self-contained HTML export — so a published report looks exactly like the
 *  one reviewed on the platform. The block vocabulary is the backend's
 *  (app/agent/blocks.py): `chart` summary|sweep|agentic, `table`
 *  setup|summary|quality|diff|scenarios, `command` baseline|<attempt>.
 *
 *  Safety: the markdown comes from an LLM. markdown-it runs with raw HTML off,
 *  every table cell is escaped, and link/image URLs are filtered, so nothing
 *  the agent writes becomes markup of its own.
 */

import hljs from 'highlight.js/lib/core'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import yaml from 'highlight.js/lib/languages/yaml'
import MarkdownIt from 'markdown-it'
import {
  agenticChart, chartHeight, mountChart, PERCENTILES, sloPercentile, summaryChart,
  sweepLatencyChart, sweepThroughputChart, type Percentile,
} from './charts'
import { labelsFor, type Comparison } from './data'
import css from './report.css?inline'
import { tr, type Lang } from './strings'
import { highlightShell } from './shell'
import { diffTable, esc, qualityTable, scenariosTable, setupTable, summaryTable } from './tables'

hljs.registerLanguage('json', json)
hljs.registerLanguage('python', python)
hljs.registerLanguage('yaml', yaml)
const SHELL = new Set(['bash', 'shell', 'sh'])

export interface ReportInput {
  markdown: string
  comparison: Comparison
  lang: Lang
  labels?: string[]
  /** An asset name (a legacy report's PNG) to a URL the page can load. */
  resolveAsset?: (name: string) => string
}

const BLOCK_KINDS = new Set(['chart', 'table', 'command'])

function injectStyle(doc: Document) {
  if (doc.getElementById('autotune-report-style')) return
  const style = doc.createElement('style')
  style.id = 'autotune-report-style'
  style.textContent = css
  doc.head.appendChild(style)
}

function parseParams(body: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of body.split('\n')) {
    const m = /^\s*([A-Za-z_]+)\s*:\s*(.*?)\s*$/.exec(line)
    if (m) out[m[1].toLowerCase()] = m[2]
  }
  return out
}

function markdownIt(resolveAsset: (name: string) => string): MarkdownIt {
  const md = new MarkdownIt({ html: false, linkify: true, typographer: false })
  md.renderer.rules.fence = (tokens, idx) => {
    const token = tokens[idx]
    const info = token.info.trim().split(/\s+/)[0] ?? ''
    if (BLOCK_KINDS.has(info)) {
      const params = esc(JSON.stringify(parseParams(token.content)))
      return `<div class="ar-block" data-kind="${info}" data-params="${params}"></div>\n`
    }
    const lang = SHELL.has(info) || hljs.getLanguage(info) ? info : ''
    const code = SHELL.has(lang)
      ? highlightShell(token.content)
      : lang
        ? hljs.highlight(token.content, { language: lang, ignoreIllegals: true }).value
        : esc(token.content)
    return codeBlock(code, token.content, lang)
  }
  const defaultImage = md.renderer.rules.image!
  md.renderer.rules.image = (tokens, idx, options, env, self) => {
    const token = tokens[idx]
    const src = token.attrGet('src') ?? ''
    if (/^[\w.-]+$/.test(src)) token.attrSet('src', resolveAsset(src))
    token.attrSet('loading', 'lazy')
    return defaultImage(tokens, idx, options, env, self)
  }
  md.renderer.rules.link_open = (tokens, idx, options, _env, self) => {
    const token = tokens[idx]
    if (!(token.attrGet('href') ?? '').startsWith('#')) {
      token.attrSet('target', '_blank')
      token.attrSet('rel', 'noopener')
    }
    return self.renderToken(tokens, idx, options)
  }
  // Blob and data URLs are how a report's own assets arrive (page / export).
  const validate = md.validateLink.bind(md)
  md.validateLink = (url: string) => /^(blob:|data:image\/)/i.test(url) || validate(url)
  return md
}

/** p50 | p90 | p99 buttons over a latency figure. */
function percentileSwitch(label: string, initial: Percentile, pick: (p: Percentile) => void): HTMLElement {
  const bar = document.createElement('div')
  bar.className = 'ar-switch'
  bar.setAttribute('role', 'group')
  bar.setAttribute('aria-label', label)
  for (const p of PERCENTILES) {
    const b = document.createElement('button')
    b.type = 'button'
    b.textContent = p
    b.setAttribute('aria-pressed', String(p === initial))
    b.addEventListener('click', () => {
      bar.querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)))
      pick(p)
    })
    bar.append(b)
  }
  return bar
}

function codeBlock(highlighted: string, raw: string, lang: string): string {
  return `<div class="ar-code"><div class="ar-code-bar"><span>${esc(lang)}</span>`
    + `<button type="button" class="ar-copy" data-copy="${esc(raw.replace(/\n$/, ''))}">copy</button></div>`
    + `<pre><code class="hljs">${highlighted}</code></pre></div>`
}

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    return
  } catch {
    /* file:// pages and older browsers: fall back */
  }
  const area = document.createElement('textarea')
  area.value = text
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  document.execCommand('copy')
  area.remove()
}

/** Render into `root`; returns a disposer (charts, observers, listeners). */
export function renderReport(root: HTMLElement, input: ReportInput): () => void {
  injectStyle(root.ownerDocument)
  const lang = input.lang
  const doc = input.comparison
  const labels = labelsFor(doc, lang, input.labels ?? [])
  const md = markdownIt(input.resolveAsset ?? ((name) => name))
  root.innerHTML = `<article class="ar">${md.render(input.markdown)}</article>`

  const disposers: (() => void)[] = []
  root.querySelectorAll<HTMLElement>('.ar-block').forEach((el) => {
    const kind = el.dataset.kind ?? ''
    const params: Record<string, string> = JSON.parse(el.dataset.params ?? '{}')
    const fail = (why: string) => {
      el.className = 'ar-block ar-error'
      el.textContent = tr(lang, 'unknownBlock', { why })
    }
    try {
      if (kind === 'table') {
        const html = {
          setup: () => setupTable(doc, lang),
          summary: () => summaryTable(doc, lang, labels),
          quality: () => qualityTable(doc, lang, labels),
          diff: () => diffTable(doc, lang, labels, Number(params.attempt ?? 1)),
          scenarios: () => scenariosTable(doc, lang),
        }[params.type as 'setup']?.()
        if (html == null) return fail(`table type ${params.type}`)
        el.innerHTML = html
      } else if (kind === 'chart') {
        const type = params.type
        const figure = (build: (w: number) => ReturnType<typeof summaryChart>) => {
          const div = document.createElement('div')
          div.className = 'ar-chart'
          el.append(div)
          const chart = mountChart(div, build, (w) => chartHeight(type, w))
          disposers.push(chart.dispose)
          return { div, chart }
        }
        if (type === 'summary') {
          figure(() => summaryChart(doc, lang, labels))
        } else if (type === 'agentic') {
          figure((w) => agenticChart(doc, lang, labels, params.scenario, w))
        } else if (type === 'sweep') {
          // two figures: throughput, then latency at one percentile with a switch
          figure((w) => sweepThroughputChart(doc, lang, labels, params.scenario, w))
          let pct: Percentile = sloPercentile(doc)
          const { div, chart } = figure((w) => sweepLatencyChart(doc, lang, labels, params.scenario, w, pct))
          div.append(percentileSwitch(tr(lang, 'percentile'), pct, (next) => {
            pct = next
            chart.update()
          }))
        } else {
          return fail(`chart type ${type}`)
        }
      } else if (kind === 'command') {
        const config = params.config ?? ''
        const run = config === 'baseline'
          ? doc.baseline
          : doc.attempts.find((a) => String(a.position) === config)
        const command = run?.launch.rendered.serve_command
        if (!command) return fail(`no serving command for ${config}`)
        el.innerHTML = codeBlock(highlightShell(command), command, 'bash')
      }
    } catch (err) {
      fail(String(err))
    }
  })

  const onClick = async (event: Event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('.ar-copy')
    if (!button) return
    await copy(button.dataset.copy ?? '')
    button.textContent = tr(lang, 'copied')
    setTimeout(() => { button.textContent = tr(lang, 'copy') }, 1500)
  }
  root.querySelectorAll<HTMLButtonElement>('.ar-copy').forEach((b) => { b.textContent = tr(lang, 'copy') })
  root.addEventListener('click', onClick)
  disposers.push(() => root.removeEventListener('click', onClick))
  return () => disposers.forEach((d) => d())
}
