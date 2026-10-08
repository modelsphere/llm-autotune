/** Reading the frozen ComparisonDocument (docs/api/agent-api.md) for display.
 *  Values and percentages are copied from it; the only arithmetic here is
 *  unit conversion (per minute to per second) and normalizing to 8 GPUs. */

import { tr, type Lang } from './strings'

/* The document is served as JSON and rendered without a schema dependency on
 * the backend; these are the fields the renderer reads. */
export interface Delta {
  baseline: number | null
  attempt: number | null
  pct: number | null
  better: 'higher' | 'lower'
  improved: boolean | null
}
export interface Level { concurrency: number; metrics: Record<string, unknown> }
export interface Scenario {
  key: string
  kind: 'sweep' | 'replay'
  label: string
  input_tokens: number | null
  output_tokens: number | null
  summary: Record<string, unknown>
  metrics: Record<string, unknown>
  levels: Level[]
  best_level: Record<string, number | null>
}
export interface Run {
  run_id: number
  label: string
  launch: {
    config: { engine: string; image: string; served_model_name: string; model_path: string }
    cards: number
    rendered: { serve_command?: string; engine_flags?: Record<string, string> }
  }
  results: {
    status: string
    headline: { quality: Record<string, number> }
    scenarios: Scenario[]
    verdicts: { module: string; metric: string; role: string }[]
  }
}
export interface ScenarioDeltas {
  key: string
  summary: Record<string, Delta>
  best_level: Record<string, Delta>
  metrics: Record<string, Delta>
}
export interface Attempt extends Run {
  position: number
  diff_vs_baseline: {
    engine?: { from: unknown; to: unknown } | null
    image?: { from: unknown; to: unknown } | null
    model_path?: { from: unknown; to: unknown } | null
    cards?: { from: unknown; to: unknown } | null
    engine_args: { added?: Record<string, unknown>; removed?: Record<string, unknown>
      changed?: Record<string, { from: unknown; to: unknown }> }
    extra_env: { added?: Record<string, unknown>; removed?: Record<string, unknown>
      changed?: Record<string, { from: unknown; to: unknown }> }
  }
  deltas: { scenarios: ScenarioDeltas[]; quality: Record<string, Delta> }
}
export interface Comparison {
  track: { model_name: string; precision: string; gpu_type: string } | null
  benchmark: {
    modules: { key: string; module_name: string; is_scenario: boolean; params: Record<string, unknown> }[]
    platform: { slo: { ttft_ms?: number; ttft_percentile?: string; min_request_output_tps?: number } }
  }
  baseline: Run
  attempts: Attempt[]
}

export function runsOf(doc: Comparison): Run[] {
  return [doc.baseline, ...doc.attempts]
}

export function labelsFor(doc: Comparison, lang: Lang, override: string[] = []): Map<number, string> {
  const runs = runsOf(doc)
  const out = new Map<number, string>()
  if (override.length === runs.length) {
    runs.forEach((r, i) => out.set(r.run_id, override[i]))
    return out
  }
  out.set(doc.baseline.run_id, tr(lang, 'baseline'))
  const many = doc.attempts.length > 1
  doc.attempts.forEach((a, i) =>
    out.set(a.run_id, many ? tr(lang, 'optimizedN', { n: i + 1 }) : tr(lang, 'optimized')))
  return out
}

export function tokens(n: unknown): string {
  const v = Number(n)
  if (n == null || Number.isNaN(v)) return '?'
  if (v >= 1000) {
    const k = v / 1000
    return Number.isInteger(k) ? `${k}k` : `${k.toFixed(1)}k`
  }
  return String(Math.round(v))
}

export function num(v: unknown): string {
  if (v == null || Number.isNaN(Number(v))) return '—'
  const x = Number(v)
  if (x === 0) return '0'
  if (Math.abs(x) >= 1000) return Math.round(x).toLocaleString('en-US')
  if (Math.abs(x) >= 100) return x.toFixed(0)
  if (Math.abs(x) >= 1) return String(Number(x.toPrecision(3)))
  return x.toFixed(3)
}

/** A percentage to one decimal, half away from zero: 119.85 is +119.9%, as a
 *  reader (or the report's prose) rounds it — `toFixed` alone reads the binary
 *  119.8499… and says 119.8. Signed, with a real minus. */
export function pctText(pct: number): string {
  const r = Math.round(Math.abs(pct) * 10 + 1e-6) / 10
  const sign = r === 0 ? '' : pct > 0 ? '+' : '−'
  return `${sign}${r.toFixed(1)}%`
}

export function concurrencyText(c: unknown): string {
  const v = Number(c)
  return Number.isFinite(v) ? String(Number(v.toPrecision(6))) : '?'
}

/** What a report calls its scenarios: a name a reader recognises ("Agent
 *  long-context") and, for the scenarios table, what it stands for. Saved with
 *  the report, per language; without it a scenario goes by its token shape. */
export interface ScenarioLabel { name?: string; description?: string }
export type ScenarioLabels = Record<string, ScenarioLabel>

export function scenarioName(s: Scenario, lang: Lang, names: ScenarioLabels = {}): string {
  const given = names[s.key]?.name
  if (given) return given
  if (s.kind === 'replay') {
    const c = s.best_level?.concurrency ?? (s.summary?.concurrency as number | undefined)
    return tr(lang, 'agentic', { c: concurrencyText(c) })
  }
  return tr(lang, 'sweep', { inp: tokens(s.input_tokens), out: tokens(s.output_tokens) })
}

export function scenarioOf(run: Run, key: string): Scenario | undefined {
  return run.results.scenarios.find((s) => s.key === key)
}

export function attemptOf(doc: Comparison, runId: number): Attempt | undefined {
  return doc.attempts.find((a) => a.run_id === runId)
}

export function scenarioDeltas(doc: Comparison, runId: number, key: string): ScenarioDeltas | undefined {
  return attemptOf(doc, runId)?.deltas.scenarios.find((d) => d.key === key)
}

/** Quality scores something is held to — a redline or a floor — and not every
 *  number the suite logs about itself. */
export function heldQuality(doc: Comparison): string[] {
  const quality = doc.baseline.results.headline.quality ?? {}
  const out: string[] = []
  for (const v of doc.baseline.results.verdicts ?? []) {
    const key = `${v.module}.${v.metric}`
    if ((v.role === 'redline' || v.role === 'quality_floor') && key in quality && !out.includes(key))
      out.push(key)
  }
  return out
}

export function cardsOf(run: Run): number {
  return Math.max(1, Number(run.launch.cards) || 1)
}

/** Every throughput a report shows is for one machine of this many GPUs —
 *  LLMBench's card-norm basis — so configs on different GPU counts compare:
 *  a 2-GPU config counts as four copies filling the machine. */
export const MACHINE_GPUS = 8

/** The machine the numbers are normalized to, as captions name it: "8 × H100". */
export function hardwareText(doc: Comparison): string {
  const card = doc.track?.gpu_type
  return card ? `${MACHINE_GPUS} × ${card}` : `${MACHINE_GPUS} GPUs`
}

/** The 8-GPU normalized throughput, from the document's per-GPU field (the
 *  document also carries it as `*_per_machine` since 2026-09-28). */
export function perMachine(perGpuValue: unknown): number | null {
  const v = Number(perGpuValue)
  return perGpuValue == null || Number.isNaN(v) ? null : v * MACHINE_GPUS
}

/** Whether every config ran on the same number of GPUs; when not, tables
 *  say how many each used. */
export function sameCards(doc: Comparison): boolean {
  const runs = runsOf(doc)
  return runs.every((r) => cardsOf(r) === cardsOf(runs[0]))
}
