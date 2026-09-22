/** The report's charts, as ECharts options built from the frozen comparison.
 *  Live in the page (tooltips, legend toggles) and identical in the HTML
 *  export, because both use this file. The browser draws the text, so a
 *  Chinese report needs no font installed anywhere. */

import { BarChart, LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import * as echarts from 'echarts/core'
import { SVGRenderer } from 'echarts/renderers'
import {
  cardsOf,
  concurrencyText,
  num,
  runsOf,
  scenarioDeltas,
  scenarioName,
  scenarioOf,
  type Comparison,
  type Delta,
} from './data'
import { tr, type Lang } from './strings'

echarts.use([
  BarChart, LineChart, GridComponent, LegendComponent, MarkLineComponent, TitleComponent,
  TooltipComponent, SVGRenderer,
])

// Baseline muted, attempts in strong colours.
const PALETTE = ['#8c9bb0', '#ee7b30', '#2f9e77', '#7a5cc2', '#d1495b', '#3a7bd5']

type Option = echarts.EChartsCoreOption

function pctLabel(d: Delta | undefined): string {
  if (!d || d.pct == null) return ''
  return `${d.pct > 0 ? '+' : ''}${d.pct.toFixed(1)}%`
}

const axisNumber = (v: number) => num(v)

function base(lang: Lang): Option {
  return {
    color: PALETTE,
    textStyle: {
      fontFamily: lang === 'zh'
        ? '-apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif'
        : '-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
    },
    animationDuration: 400,
    tooltip: { confine: true },
  }
}

export function summaryChart(doc: Comparison, lang: Lang, labels: Map<number, string>): Option {
  const scenarios = doc.baseline.results.scenarios
  const runs = runsOf(doc)
  return {
    ...base(lang),
    title: { text: tr(lang, 'summaryTitle'), left: 'center', textStyle: { fontSize: 14, fontWeight: 600 } },
    legend: { bottom: 0 },
    grid: { left: 64, right: 24, top: 56, bottom: 64 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, confine: true,
      valueFormatter: (v: number) => `${num(v)} ${tr(lang, 'tpsPerGpu')}` },
    xAxis: { type: 'category', data: scenarios.map((s) => scenarioName(s, lang)),
      axisLabel: { interval: 0, width: 140, overflow: 'break' } },
    yAxis: { type: 'value', name: tr(lang, 'totalPerGpu'), nameLocation: 'middle', nameGap: 52,
      axisLabel: { formatter: axisNumber } },
    series: runs.map((r, i) => ({
      type: 'bar',
      name: labels.get(r.run_id) ?? r.label,
      barMaxWidth: 56,
      data: scenarios.map((s) => {
        const value = scenarioOf(r, s.key)?.best_level?.total_tps_per_gpu ?? null
        const d = i ? scenarioDeltas(doc, r.run_id, s.key)?.best_level?.total_tps_per_gpu : undefined
        return { value, label: { show: i > 0 && !!d, formatter: pctLabel(d) } }
      }),
      label: { position: 'top', fontWeight: 600, fontSize: 12 },
    })),
  }
}

type Level = { concurrency: number; metrics: Record<string, unknown> }

export const PERCENTILES = ['p50', 'p90', 'p99'] as const
export type Percentile = (typeof PERCENTILES)[number]

interface Panel {
  title: string
  unit: string
  value: (lv: Level, cards: number) => number
  /** a latency: the tooltip lists this metric's p50/p90/p99, the shown one bold */
  latency?: { prefix: string; shown: Percentile }
  /** draw the TTFT SLO line (when the shown percentile is the SLO's) */
  slo?: boolean
}

type Point = { value: [number, number]; all?: Record<string, number> }

/** Two panels side by side (stacked when narrow) over one sweep, one line per
 *  config. A legend entry toggles its config in both. */
function sweepPair(doc: Comparison, lang: Lang, labels: Map<number, string>, key: string,
  width: number, title: string, subtitle: string, panels: [Panel, Panel], bestMarks: boolean): Option {
  const runs = runsOf(doc)
  const slo = doc.benchmark.platform.slo ?? {}
  const sloPct = slo.ttft_percentile ?? 'p50'
  const narrow = width < 720
  const top = 76
  const grids = narrow
    ? [{ left: 64, right: 20, top, height: 200 }, { left: 64, right: 20, top: top + 270, height: 200 }]
    : [{ left: 64, right: '53%', top, bottom: 64 }, { left: '57%', right: 20, top, bottom: 64 }]
  const series: object[] = []
  runs.forEach((r, i) => {
    const s = scenarioOf(r, key)
    if (!s?.levels?.length) return
    const levels = [...s.levels].sort((a, b) => a.concurrency - b.concurrency)
    const name = labels.get(r.run_id) ?? r.label
    const color = PALETTE[i % PALETTE.length]
    const best = s.best_level?.concurrency
    panels.forEach((panel, axis) => {
      const marks: object[] = []
      if (bestMarks && best != null) {
        marks.push({ xAxis: best, label: { show: false }, lineStyle: { color, type: 'dashed', width: 1.5 } })
      }
      if (panel.slo && i === 0 && slo.ttft_ms && panel.latency?.shown === sloPct) {
        marks.push({ yAxis: slo.ttft_ms, lineStyle: { color: '#d1495b', type: 'dashed' },
          label: { show: true, position: 'insideStartTop', color: '#d1495b',
            formatter: tr(lang, 'sloLine', { pct: sloPct, ms: Math.round(slo.ttft_ms).toLocaleString('en-US') }) } })
      }
      series.push({
        type: 'line', name, xAxisIndex: axis, yAxisIndex: axis, color, symbolSize: 6,
        data: levels.map((lv): Point => ({
          value: [lv.concurrency, panel.value(lv, cardsOf(r))],
          all: panel.latency && Object.fromEntries(PERCENTILES.map((p) =>
            [p, Number(lv.metrics[`${panel.latency!.prefix}_${p}_ms`] ?? NaN)])),
        })),
        markLine: marks.length ? { silent: true, symbol: 'none', data: marks } : undefined,
      })
    })
  })
  const xAxis = (gridIndex: number) => ({
    type: 'log', logBase: 2, gridIndex, name: tr(lang, 'concurrency'), nameLocation: 'middle',
    nameGap: 28, axisLabel: { formatter: (v: number) => concurrencyText(v) },
    splitLine: { show: true, lineStyle: { opacity: 0.4 } },
  })
  const yAxis = (gridIndex: number) => ({
    type: 'value', gridIndex, name: panels[gridIndex].title,
    nameLocation: 'middle', nameGap: 52, axisLabel: { formatter: axisNumber },
  })
  return {
    ...base(lang),
    title: [
      { text: title, left: 'center', textStyle: { fontSize: 14, fontWeight: 600 } },
      { text: subtitle, left: 'center', top: 22, textStyle: { fontSize: 11, fontWeight: 400, color: '#888' } },
    ],
    legend: { bottom: 0, data: runs.map((r) => labels.get(r.run_id) ?? r.label) },
    tooltip: {
      trigger: 'item', confine: true,
      formatter: (p: { seriesName: string; data: Point; seriesIndex: number }) => {
        const panel = panels[p.seriesIndex % 2]
        const [c, v] = p.data.value
        const head = `${p.seriesName}<br>${tr(lang, 'concurrency')} ${concurrencyText(c)}`
        if (!panel.latency || !p.data.all) return `${head}: <b>${num(v)}</b> ${panel.unit}`
        const shown = panel.latency.shown
        return head + PERCENTILES.map((q) => {
          const text = `${q}: ${num(p.data.all![q])} ${panel.unit}`
          return `<br>${q === shown ? `<b>${text}</b>` : text}`
        }).join('')
      },
    },
    grid: grids,
    xAxis: [xAxis(0), xAxis(1)],
    yAxis: [yAxis(0), yAxis(1)],
    series,
  }
}

const perGpu = (name: string) => (lv: Level, cards: number) => Number(lv.metrics[name] ?? NaN) / cards
const metric = (name: string) => (lv: Level) => Number(lv.metrics[name] ?? NaN)

function sweepTitle(doc: Comparison, lang: Lang, key: string): string {
  const scenario = scenarioOf(doc.baseline, key)
  return scenario ? scenarioName(scenario, lang) : key
}

/** A sweep's first figure: total and output throughput per GPU by concurrency. */
export function sweepThroughputChart(doc: Comparison, lang: Lang, labels: Map<number, string>,
  key: string, width: number): Option {
  return sweepPair(doc, lang, labels, key, width,
    tr(lang, 'sweepThroughput', { name: sweepTitle(doc, lang, key) }), tr(lang, 'bestMarks'), [
      { title: tr(lang, 'totalPanel'), unit: tr(lang, 'tpsPerGpu'), value: perGpu('total_tps_mean') },
      { title: tr(lang, 'outputPanel'), unit: tr(lang, 'tpsPerGpu'), value: perGpu('output_tps_mean') },
    ], true)
}

/** The percentile a sweep's latency figure opens on: the one the SLO uses. */
export function sloPercentile(doc: Comparison): Percentile {
  const pct = doc.benchmark.platform.slo?.ttft_percentile
  return (PERCENTILES as readonly string[]).includes(pct ?? '') ? pct as Percentile : 'p50'
}

/** A sweep's second figure: TTFT (with the SLO) and TPOT at one percentile,
 *  switched by the buttons the renderer puts over it. */
export function sweepLatencyChart(doc: Comparison, lang: Lang, labels: Map<number, string>,
  key: string, width: number, pct: Percentile): Option {
  return sweepPair(doc, lang, labels, key, width,
    tr(lang, 'sweepLatency', { name: sweepTitle(doc, lang, key) }), '', [
      { title: tr(lang, 'ttftAxis', { pct }), unit: 'ms', value: metric(`ttft_${pct}_ms`),
        latency: { prefix: 'ttft', shown: pct }, slo: true },
      { title: tr(lang, 'tpotAxis', { pct }), unit: 'ms', value: metric(`tpot_${pct}_ms`),
        latency: { prefix: 'tpot', shown: pct } },
    ], false)
}

export function agenticChart(doc: Comparison, lang: Lang, labels: Map<number, string>,
  key: string, width: number): Option {
  const runs = runsOf(doc)
  const narrow = width < 720
  const tput: [string, string][] = [
    ['uncached_input_tpm', tr(lang, 'inputUncached')],
    ['cached_tpm', tr(lang, 'inputCached')],
    ['output_tpm', tr(lang, 'output')],
  ]
  const ttft: [string, string][] = [['ttft_p50_ms', 'p50'], ['ttft_p90_ms', 'p90'], ['ttft_p99_ms', 'p99']]
  const grids = narrow
    ? [{ left: 64, right: 20, top: 64, height: 200 }, { left: 64, right: 20, top: 330, height: 200 }]
    : [{ left: 64, right: '53%', top: 64, bottom: 64 }, { left: '57%', right: 20, top: 64, bottom: 64 }]
  const scenario = scenarioOf(doc.baseline, key)
  const series: object[] = []
  runs.forEach((r, i) => {
    const metrics = scenarioOf(r, key)?.metrics ?? {}
    const deltas = scenarioDeltas(doc, r.run_id, key)?.metrics ?? {}
    const name = labels.get(r.run_id) ?? r.label
    const bar = (keys: [string, string][], scale: number, axis: number) => ({
      type: 'bar', name, xAxisIndex: axis, yAxisIndex: axis, barMaxWidth: 44,
      data: keys.map(([k]) => {
        const v = metrics[k]
        const d = i ? deltas[k] : undefined
        return { value: v == null ? null : Number(v) / scale, label: { show: i > 0 && !!d, formatter: pctLabel(d) } }
      }),
      label: { position: 'top', fontWeight: 600, fontSize: 11 },
    })
    series.push(bar(tput, 60 * cardsOf(r), 0), bar(ttft, 1, 1))
  })
  return {
    ...base(lang),
    title: [
      { text: scenario ? scenarioName(scenario, lang) : key, left: 'center', textStyle: { fontSize: 14, fontWeight: 600 } },
    ],
    legend: { bottom: 0, data: runs.map((r) => labels.get(r.run_id) ?? r.label) },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, confine: true, valueFormatter: (v: number) => num(v) },
    grid: grids,
    xAxis: [
      { type: 'category', gridIndex: 0, data: tput.map(([, l]) => l), axisLabel: { interval: 0 } },
      { type: 'category', gridIndex: 1, data: ttft.map(([, l]) => `TTFT ${l}`) },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, name: tr(lang, 'throughputPanel') + ` (${tr(lang, 'tpsPerGpu')})`,
        nameLocation: 'middle', nameGap: 52, axisLabel: { formatter: axisNumber } },
      { type: 'value', gridIndex: 1, name: tr(lang, 'ttftPanel'), nameLocation: 'middle', nameGap: 52,
        axisLabel: { formatter: axisNumber } },
    ],
    series,
  }
}

export function chartHeight(type: string, width: number): number {
  if (type === 'summary') return 380
  return width < 720 ? 640 : 400
}

export interface MountedChart {
  dispose: () => void
  /** rebuild the option in place (e.g. after a percentile switch) */
  update: () => void
}

export function mountChart(
  el: HTMLElement, build: (width: number) => Option, heightFor: (width: number) => number,
): MountedChart {
  let width = el.clientWidth || 800
  el.style.height = `${heightFor(width)}px`
  const chart = echarts.init(el, undefined, { renderer: 'svg' })
  chart.setOption(build(width))
  const observer = typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(() => {
        const w = el.clientWidth
        if (!w) return
        const crossed = (w < 720) !== (width < 720)
        width = w
        if (crossed) {
          el.style.height = `${heightFor(w)}px`
          chart.setOption(build(w), true)
        }
        chart.resize()
      })
    : null
  observer?.observe(el)
  return {
    dispose: () => {
      observer?.disconnect()
      chart.dispose()
    },
    update: () => chart.setOption(build(width), true),
  }
}
