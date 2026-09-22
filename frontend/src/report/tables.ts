/** The report's tables, as HTML strings, drawn from the frozen comparison.
 *  Every cell is escaped; numbers right-aligned; a change carries its
 *  verdict (better / worse) as a colour, never as a guess. */

import {
  attemptOf,
  cardsOf,
  concurrencyText,
  heldQuality,
  num,
  runsOf,
  scenarioDeltas,
  scenarioName,
  scenarioOf,
  tokens,
  type Comparison,
  type Delta,
} from './data'
import { tr, type Lang } from './strings'

export function esc(text: unknown): string {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function pctBadge(d: Delta | undefined): string {
  if (!d || d.pct == null) return ''
  const cls = d.improved === true ? 'good' : d.improved === false ? 'bad' : 'flat'
  const sign = d.pct > 0 ? '+' : d.pct < 0 ? '−' : ''
  return ` <span class="ar-pct ar-${cls}">${sign}${Math.abs(d.pct).toFixed(1)}%</span>`
}

/** A line under a table saying how to read it. */
function note(text: string): string {
  return `<p class="ar-note">${esc(text)}</p>`
}

function table(headers: string[], rows: string[][], numericFrom = 1): string {
  const head = headers
    .map((h, i) => `<th class="${i >= numericFrom ? 'ar-num' : ''}">${esc(h)}</th>`).join('')
  const body = rows.map((r) =>
    `<tr>${r.map((c, i) => `<td class="${i >= numericFrom ? 'ar-num' : ''}">${c}</td>`).join('')}</tr>`,
  ).join('')
  return `<div class="ar-table-wrap"><table class="ar-table"><thead><tr>${head}</tr></thead>`
    + `<tbody>${body}</tbody></table></div>`
}

export function summaryTable(doc: Comparison, lang: Lang, labels: Map<number, string>): string {
  const runs = runsOf(doc)
  const rows = doc.baseline.results.scenarios.map((base) => {
    const cells = runs.map((r) => {
      const best = scenarioOf(r, base.key)?.best_level ?? {}
      if (r.results.status !== 'measured' || best.total_tps_per_gpu == null)
        return `<span class="ar-muted">${esc(tr(lang, 'noData'))}</span>`
      const d = scenarioDeltas(doc, r.run_id, base.key)?.best_level?.total_tps_per_gpu
      return `<b>${esc(num(best.total_tps_per_gpu))}</b> ${esc(tr(lang, 'tpsPerGpu'))}`
        + `<div class="ar-sub">${esc(tr(lang, 'atConcurrency', { c: concurrencyText(best.concurrency) }))}`
        + `${pctBadge(d)}</div>`
    })
    return [esc(scenarioName(base, lang)), ...cells]
  })
  return table([tr(lang, 'scenario'), ...runs.map((r) => labels.get(r.run_id) ?? r.label)], rows)
}

export function qualityTable(doc: Comparison, lang: Lang, labels: Map<number, string>): string {
  const runs = runsOf(doc)
  const rows = heldQuality(doc).map((key) => [
    `<code>${esc(key.split('.').pop())}</code>`,
    ...runs.map((r) => {
      const value = r.results.headline.quality?.[key]
      const d = attemptOf(doc, r.run_id)?.deltas.quality?.[key]
      return `${esc(num(value))}${pctBadge(d)}`
    }),
  ])
  if (!rows.length) return ''
  return table([tr(lang, 'benchmark'), ...runs.map((r) => labels.get(r.run_id) ?? r.label)], rows)
    + note(tr(lang, 'qualityNote'))
}

export function diffTable(doc: Comparison, lang: Lang, labels: Map<number, string>, position: number): string {
  const a = doc.attempts.find((x) => x.position === position)
  if (!a) return ''
  const d = a.diff_vs_baseline
  const flags = a.launch.rendered.engine_flags ?? {}
  const rows: string[][] = []
  const cell = (v: unknown) => (v == null ? '—' : `<code>${esc(v)}</code>`)
  for (const name of ['engine', 'image', 'model_path', 'cards'] as const) {
    const change = d[name]
    if (change) rows.push([esc(name), cell(change.from), cell(change.to)])
  }
  const args = d.engine_args ?? {}
  for (const [k, v] of Object.entries(args.added ?? {})) rows.push([`<code>${esc(flags[k] ?? k)}</code>`, '—', cell(v)])
  for (const [k, v] of Object.entries(args.removed ?? {})) rows.push([`<code>${esc(flags[k] ?? k)}</code>`, cell(v), '—'])
  for (const [k, v] of Object.entries(args.changed ?? {}))
    rows.push([`<code>${esc(flags[k] ?? k)}</code>`, cell(v.from), cell(v.to)])
  const env = d.extra_env ?? {}
  for (const [k, v] of Object.entries(env.added ?? {})) rows.push([`<code>${esc(k)}</code>`, '—', cell(v)])
  for (const [k, v] of Object.entries(env.removed ?? {})) rows.push([`<code>${esc(k)}</code>`, cell(v), '—'])
  for (const [k, v] of Object.entries(env.changed ?? {}))
    rows.push([`<code>${esc(k)}</code>`, cell(v.from), cell(v.to)])
  if (!rows.length) return `<p class="ar-muted">${esc(tr(lang, 'identical'))}</p>`
  rows.sort((x, y) => x[0].localeCompare(y[0]))
  return table([
    tr(lang, 'setting'),
    labels.get(doc.baseline.run_id) ?? doc.baseline.label,
    labels.get(a.run_id) ?? a.label,
  ], rows, 3)
}

export function scenariosTable(doc: Comparison, lang: Lang): string {
  const modules = new Map(doc.benchmark.modules.map((m) => [m.key, m]))
  const rows: string[][] = doc.baseline.results.scenarios.map((s) => {
    const params = modules.get(s.key)?.params ?? {}
    if (s.kind === 'replay') {
      return [
        esc(scenarioName(s, lang)),
        esc(tr(lang, 'workloadAgentic', { n: String(params.max_samples ?? '?') })),
        esc(tr(lang, 'methodAgentic', { c: String(params.concurrency ?? '?') })),
      ]
    }
    return [
      esc(scenarioName(s, lang)),
      esc(tr(lang, 'workloadSweep', { inp: tokens(params.input_tokens), out: tokens(params.output_tokens) })),
      esc(tr(lang, 'methodSweep', { max: String(params.search_max_concurrency ?? '?') })),
    ]
  })
  const held = heldQuality(doc).map((k) => k.split('.').pop())
  if (held.length) rows.push([esc(tr(lang, 'quality')), esc(held.join(', ')), esc(tr(lang, 'accuracy'))])
  const kinds = new Set(doc.baseline.results.scenarios.map((s) => s.kind))
  const notes = [
    ...(kinds.has('sweep') ? [tr(lang, 'sweepNote')] : []),
    ...(kinds.has('replay') ? [tr(lang, 'agenticNote')] : []),
  ]
  return table([tr(lang, 'scenario'), tr(lang, 'workload'), tr(lang, 'method')], rows, 3)
    + (notes.length ? note(notes.join(' ')) : '')
}

export function setupTable(doc: Comparison, lang: Lang): string {
  const slo = doc.benchmark.platform.slo ?? {}
  const rows: string[][] = []
  const track = doc.track
  if (track?.model_name) rows.push([esc(tr(lang, 'model')), esc(track.model_name)])
  if (track?.precision) rows.push([esc(tr(lang, 'precision')), esc(track.precision)])
  rows.push([esc(tr(lang, 'hardware')), esc(tr(lang, 'hardwareValue', {
    n: cardsOf(doc.baseline), card: track?.gpu_type ?? '',
  }))])
  const images = [...new Set(runsOf(doc).map((r) => r.launch.config.image).filter(Boolean))]
  if (images.length) rows.push([esc(tr(lang, 'image')), images.map((i) => `<code>${esc(i)}</code>`).join('<br>')])
  if (slo.ttft_ms) {
    rows.push([esc(tr(lang, 'slo')), esc(tr(lang, 'sloValue', {
      pct: slo.ttft_percentile ?? 'p50',
      ms: Math.round(slo.ttft_ms).toLocaleString('en-US'),
      tps: slo.min_request_output_tps ?? 0,
    }))])
  }
  return table([tr(lang, 'item'), tr(lang, 'value')], rows, 2)
}
