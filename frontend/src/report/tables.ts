/** The report's tables, as HTML strings, drawn from the frozen comparison.
 *  Every cell is escaped; numbers right-aligned; a change carries its
 *  verdict (better / worse) as a colour, never as a guess. */

import {
  attemptOf,
  cardsOf,
  concurrencyText,
  heldQuality,
  num,
  pctText,
  perMachine,
  runsOf,
  sameCards,
  scenarioDeltas,
  scenarioName,
  scenarioOf,
  tokens,
  type Comparison,
  type Delta,
  type ScenarioLabels,
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
  return ` <span class="ar-pct ar-${cls}">${pctText(d.pct)}</span>`
}

/** A line under a table saying how to read it. */
function note(text: string): string {
  return `<p class="ar-note">${esc(text)}</p>`
}

function table(headers: string[], rows: string[][], numericFrom = 1, cls = ''): string {
  const head = headers
    .map((h, i) => `<th class="${i >= numericFrom ? 'ar-num' : ''}">${esc(h)}</th>`).join('')
  const body = rows.map((r) =>
    `<tr>${r.map((c, i) => `<td class="${i >= numericFrom ? 'ar-num' : ''}">${c}</td>`).join('')}</tr>`,
  ).join('')
  return `<div class="ar-table-wrap"><table class="ar-table${cls ? ` ${cls}` : ''}">`
    + `<thead><tr>${head}</tr></thead>`
    + `<tbody>${body}</tbody></table></div>`
}

export function summaryTable(doc: Comparison, lang: Lang, labels: Map<number, string>,
  names: ScenarioLabels = {}): string {
  const runs = runsOf(doc)
  const mixed = !sameCards(doc)
  const rows: string[][] = []
  for (const base of doc.baseline.results.scenarios) {
    for (const r of runs) {
      const best = scenarioOf(r, base.key)?.best_level ?? {}
      const name = esc(scenarioName(base, lang, names))
      const config = esc(labels.get(r.run_id) ?? r.label)
      const gpus = mixed ? [esc(cardsOf(r))] : []
      if (r.results.status !== 'measured' || best.total_tps_per_gpu == null) {
        rows.push([name, config, ...gpus, `<span class="ar-muted">${esc(tr(lang, 'noData'))}</span>`, ''])
        continue
      }
      const d = scenarioDeltas(doc, r.run_id, base.key)?.best_level?.total_tps_per_gpu
      rows.push([
        name,
        config,
        ...gpus,
        esc(concurrencyText(best.concurrency)),
        `<b>${esc(num(perMachine(best.total_tps_per_gpu)))}</b>${pctBadge(d)}`,
      ])
    }
  }
  return table([
    tr(lang, 'scenario'), tr(lang, 'config'), ...(mixed ? [tr(lang, 'gpusCol')] : []),
    tr(lang, 'bestConcurrency'), tr(lang, 'totalServing'),
  ], rows, 2) + note(tr(lang, 'capacityNote'))
}

/** A quality score read as a percentage when it is a 0–1 accuracy, which is
 *  what every question-set score on the platform is. */
function isRatio(values: (number | undefined)[]): boolean {
  const seen = values.filter((v): v is number => typeof v === 'number' && !Number.isNaN(v))
  return seen.length > 0 && seen.every((v) => v >= 0 && v <= 1)
}

function score(v: number | undefined, ratio: boolean): string {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return ratio ? `${(Number(v) * 100).toFixed(2)}%` : num(v)
}

export function qualityTable(doc: Comparison, lang: Lang, labels: Map<number, string>): string {
  const runs = runsOf(doc)
  const keys = heldQuality(doc)
  if (!keys.length) return ''
  const name = (r: { run_id: number; label: string }) => labels.get(r.run_id) ?? r.label
  // One attempt: value, value, how much it moved, and by what share. Several:
  // one column per config, each carrying its own change.
  if (doc.attempts.length === 1) {
    const attempt = doc.attempts[0]
    const rows = keys.map((key) => {
      const b = doc.baseline.results.headline.quality?.[key]
      const a = attempt.results.headline.quality?.[key]
      const d = attempt.deltas.quality?.[key]
      const ratio = isRatio([b, a])
      const moved = b == null || a == null ? null : Number(a) - Number(b)
      const abs = moved == null ? '—'
        : ratio
          ? `${moved > 0 ? '+' : '−'}${Math.abs(moved * 100).toFixed(2)} ${tr(lang, 'points')}`
          : `${moved > 0 ? '+' : '−'}${num(Math.abs(moved))}`
      return [
        `<code>${esc(key.split('.').pop())}</code>`,
        esc(score(b, ratio)),
        esc(score(a, ratio)),
        esc(abs),
        (pctBadge(d) || '—').trim(),
      ]
    })
    return table([
      tr(lang, 'metric'), name(doc.baseline), name(attempt), tr(lang, 'absChange'), tr(lang, 'relChange'),
    ], rows)
      + note(tr(lang, 'qualityNote'))
  }
  const rows = keys.map((key) => {
    const ratio = isRatio(runs.map((r) => r.results.headline.quality?.[key]))
    return [
      `<code>${esc(key.split('.').pop())}</code>`,
      ...runs.map((r) => {
        const value = r.results.headline.quality?.[key]
        const d = attemptOf(doc, r.run_id)?.deltas.quality?.[key]
        return `${esc(score(value, ratio))}${pctBadge(d)}`
      }),
    ]
  })
  return table([tr(lang, 'metric'), ...runs.map(name)], rows)
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
  ], rows, 3) + note(tr(lang, 'diffNote'))
}

export function scenariosTable(doc: Comparison, lang: Lang, names: ScenarioLabels = {}): string {
  const modules = new Map(doc.benchmark.modules.map((m) => [m.key, m]))
  const rows: string[][] = doc.baseline.results.scenarios.map((s) => {
    const params = modules.get(s.key)?.params ?? {}
    const given = names[s.key]?.description
    if (s.kind === 'replay') {
      const c = String(params.concurrency ?? s.best_level?.concurrency ?? '?')
      return [
        `<b>${esc(scenarioName(s, lang, names))}</b>`,
        esc(tr(lang, 'workloadAgentic', { n: String(params.max_samples ?? '?') })),
        esc(given ?? tr(lang, 'defaultAgentic', { c })),
      ]
    }
    return [
      `<b>${esc(scenarioName(s, lang, names))}</b>`,
      esc(tr(lang, 'ioValue', { inp: tokens(params.input_tokens ?? s.input_tokens),
        out: tokens(params.output_tokens ?? s.output_tokens) })),
      esc(given ?? tr(lang, 'defaultSweep', { max: String(params.search_max_concurrency ?? '?') })),
    ]
  })
  const held = heldQuality(doc).map((k) => k.split('.').pop())
  if (held.length) {
    rows.push([
      `<b>${esc(tr(lang, 'quality'))}</b> (${esc(held.join(', '))})`,
      esc(tr(lang, 'qualityWorkload')),
      esc(tr(lang, 'accuracy')),
    ])
  }
  const kinds = new Set(doc.baseline.results.scenarios.map((s) => s.kind))
  const notes = [
    ...(kinds.has('sweep') ? [tr(lang, 'randomNote')] : []),
    ...(kinds.has('replay') ? [tr(lang, 'agenticNote')] : []),
  ]
  return table([tr(lang, 'scenario'), tr(lang, 'ioTokens'), tr(lang, 'stands')], rows, 3,
    'ar-table-defs ar-table-scenarios')
    + (notes.length ? note(notes.join(' ')) : '')
}

/** What the serving has to hold to (the SLO) and what is maximized while it
 *  holds — the definitions a reader needs before any number means anything. */
export function sloTable(doc: Comparison, lang: Lang): string {
  const slo = doc.benchmark.platform.slo ?? {}
  const pct = slo.ttft_percentile ?? 'p50'
  const rows: string[][] = []
  if (slo.ttft_ms) {
    rows.push([
      esc(tr(lang, 'slo')),
      `<b>${esc(tr(lang, 'ttftMetric', { pct }))}</b>`,
      `<b>${esc(tr(lang, 'ttftLimit', { ms: Math.round(slo.ttft_ms).toLocaleString('en-US') }))}</b>`,
      esc(tr(lang, 'ttftDefinition', { pct })),
    ])
  }
  if (slo.min_request_output_tps) {
    rows.push([
      esc(tr(lang, 'slo')),
      `<b>${esc(tr(lang, 'otpsMetric'))}</b>`,
      `<b>${esc(tr(lang, 'otpsLimit', { tps: slo.min_request_output_tps }))}</b>`,
      esc(tr(lang, 'otpsDefinition')),
    ])
  }
  rows.push([
    esc(tr(lang, 'objective')),
    `<b>${esc(tr(lang, 'objectiveMetric'))}</b>`,
    `<b>${esc(tr(lang, 'objectiveLimit'))}</b>`,
    esc(tr(lang, 'objectiveDefinition')),
  ])
  return table([tr(lang, 'category'), tr(lang, 'metric'), tr(lang, 'constraint'), tr(lang, 'definition')],
    rows, 4, 'ar-table-defs ar-table-slo')
    + note(tr(lang, 'sloNote'))
}

export function setupTable(doc: Comparison, lang: Lang, labels: Map<number, string>): string {
  const slo = doc.benchmark.platform.slo ?? {}
  const rows: string[][] = []
  const group = doc.group
  if (group?.model_name) rows.push([esc(tr(lang, 'model')), esc(group.model_name)])
  if (group?.precision) rows.push([esc(tr(lang, 'precision')), esc(group.precision)])
  const card = group?.gpu_type ?? ''
  const hardware = sameCards(doc)
    ? tr(lang, 'hardwareValue', { n: cardsOf(doc.baseline), card })
    : runsOf(doc).map((r) => `${labels.get(r.run_id) ?? r.label}: ${cardsOf(r)}× ${card}`).join(' · ')
  rows.push([esc(tr(lang, 'hardware')), esc(hardware)])
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
