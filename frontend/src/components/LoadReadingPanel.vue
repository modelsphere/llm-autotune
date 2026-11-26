<script setup lang="ts">
/** How loaded the dataset's backend is, read by LLMBench from a week of
 *  gateway logs: a verdict in words, where it sits on the idle–moderate–busy
 *  scale, the test that fits it, and the curve it was read from (the same
 *  request getting slower as more run at once, and how many requests ran at
 *  each level). */
import { computed } from 'vue'
import type { LoadReading, LoadReadingGroup } from '../api/client'
import { useI18n } from '../i18n'
import InfoHint from './InfoHint.vue'

const props = defineProps<{
  reading: LoadReading
  /** In a form: the scale and the chart, with every number drawn into the
   *  chart and no sentences — the form says what to do with the reading. */
  compact?: boolean
}>()
const { t } = useI18n()

const r = computed(() => props.reading)
const groups = computed<LoadReadingGroup[]>(() => r.value.groups ?? [])
const idleBelow = computed(() => r.value.thresholds?.idle_below ?? 0.1)
const busyFrom = computed(() => r.value.thresholds?.busy_from ?? 0.5)
const w = computed(() => Math.max(0, Math.min(1, r.value.w_log ?? 0)))
const share = computed(() => `${Math.round(w.value * 100)}%`)
const hasScale = computed(() => r.value.w_log !== undefined && r.value.status !== 'failed')
const pct = (v: number) => `${(Math.max(0, Math.min(1, v)) * 100).toFixed(1)}%`

const hard = computed(() => r.value.fit?.hard_checks ?? Object.keys(r.value.fit?.checks ?? {}))
const failedChecks = computed(() =>
  Object.entries(r.value.fit?.checks ?? {})
    .filter(([name, ok]) => ok === false && hard.value.includes(name))
    .map(([name]) => t(`datasets.load.checkShort.${name}`))
    .join(', '))

const sentence = computed(() => {
  const x = r.value
  if (x.status === 'failed') return t('datasets.load.failed', { error: x.error ?? '' })
  if (x.status === 'no_traffic' || x.status === 'too_little_traffic') return t('datasets.load.tooLittle', { note: x.note ?? '' })
  if (x.category === 'unclear') {
    return t('datasets.load.unclear', {
      checks: failedChecks.value,
      fitted: x.fitted_category ? t(`datasets.load.cat.${x.fitted_category}`) : '—',
    })
  }
  if (!groups.value.length) return t('datasets.load.idleNoOverlap', { share: `${Math.round((x.overlapped_share ?? 0) * 100)}%` })
  if (x.knee_group == null) return t('datasets.load.noKnee', { max: groups.value[groups.value.length - 1].group })
  return t(`datasets.load.say.${x.category}`, { share: share.value, knee: x.knee_group })
})

/** The test this load calls for; nothing when the reading cannot be trusted. */
const fits = computed(() => {
  const c = r.value.category
  if (c === 'idle') return t('datasets.load.fits.idle')
  if (c === 'busy') return t('datasets.load.fits.busy')
  if (c === 'moderate') {
    const thr = Math.round(w.value * 100)
    return t('datasets.load.fits.moderate', { lat: 100 - thr, thr })
  }
  return ''
})

// --- the two charts ----------------------------------------------------------
// Side by side, sharing one x axis (concurrency): on the left how much slower
// a same-size request gets at each concurrency, on the right how much of the
// traffic ran there. The saturation point is marked on both.
const CW = 300
const L = 30
const R = 8
const PT = 24
const PH = 110
const BASE = PT + PH
const band = computed(() => (CW - L - R) / Math.max(1, groups.value.length))
const barW = computed(() => Math.max(4, Math.min(20, band.value - 4)))
const x = (i: number) => L + band.value * (i + 0.5)
const sMax = computed(() => Math.max(1.2, ...groups.value.map((g) => g.slowdown)))
const sy = (v: number) => BASE - ((v - 1) / (sMax.value - 1)) * PH
const sTicks = computed(() => {
  const step = sMax.value > 4 ? 1 : 0.5
  const out: number[] = []
  for (let v = 1; v <= sMax.value + 1e-9; v += step) out.push(+v.toFixed(1))
  return out
})
const line = computed(() => groups.value.map((g, i) => `${x(i)},${sy(g.slowdown)}`).join(' '))
const nMax = computed(() => Math.max(1, ...groups.value.map((g) => g.requests)))
const by = (n: number) => BASE - (n / nMax.value) * PH * 0.9
const kneeIndex = computed(() => {
  const k = r.value.knee_group
  if (k == null) return null
  const i = groups.value.findIndex((g) => g.group === k)
  return i < 0 ? null : i
})
const kneeX = computed(() => (kneeIndex.value === null ? 0 : x(kneeIndex.value) - band.value / 2))
/** A label beside the saturation line, on whichever side has room. */
const kneeLabel = computed(() => kneeX.value > CW - 110
  ? { x: kneeX.value - 4, anchor: 'end' } : { x: kneeX.value + 4, anchor: 'start' })
const past = (g: LoadReadingGroup) => r.value.knee_group != null && g.group >= r.value.knee_group
const folded = computed(() => {
  const last = groups.value[groups.value.length - 1]
  return !!last && (r.value.c_req_max ?? 0) > last.group * 1.15
})
const tickLabel = (g: LoadReadingGroup, i: number) => (i === groups.value.length - 1 && folded.value ? `${g.group}+` : `${g.group}`)
/** Where a concurrency falls on the x axis: between the two groups around it. */
function xAt(c: number): number {
  const gs = groups.value
  if (!gs.length) return L
  if (c <= gs[0].group) return x(0)
  for (let i = 0; i < gs.length - 1; i++) {
    const a = gs[i].group
    const b = gs[i + 1].group
    if (c <= b) return x(i) + ((c - a) / (b - a)) * (x(i + 1) - x(i))
  }
  return x(gs.length - 1)
}
/** Typical and busiest-hours concurrency, marked under the traffic chart's
 *  axis; the second drops a row when the two labels would touch. */
const marks = computed(() => {
  const out: { x: number; row: number; text: string; cls: string }[] = []
  const typ = r.value.c_typ
  const peak = r.value.c_peak
  if (typ != null) out.push({ x: xAt(typ), row: 0, text: t('datasets.load.typicalMark', { c: typ.toFixed(1) }), cls: 'typ' })
  if (peak != null) {
    const px = xAt(peak)
    const row = out.length && Math.abs(px - out[0].x) < 80 ? 1 : 0
    out.push({ x: px, row, text: t('datasets.load.busiestMark', { c: peak.toFixed(1) }), cls: 'peak' })
  }
  return out
})
const markAnchor = (mx: number) => (mx > CW - 50 ? 'end' : mx < L + 40 ? 'start' : 'middle')
const MARK_Y = BASE + 30
const H = computed(() => MARK_Y + 12 * (1 + Math.max(0, ...marks.value.map((m) => m.row))) + 16)
const tip = (g: LoadReadingGroup) => t('datasets.load.tip', {
  group: g.group,
  requests: g.requests.toLocaleString(),
  share: `${Math.round(g.share_from * 100)}%`,
  slowdown: g.slowdown.toFixed(2),
  raw: g.raw_slowdown.toFixed(2),
  out: Math.round(g.output_mean),
  prompt: Math.round(g.prompt_mean).toLocaleString(),
})
const fmtW = (w?: number) => (w == null ? '—' : `${Math.round(w * 100)}%`)
const checks = computed(() => Object.entries(r.value.fit?.checks ?? {}))
const checkArgs = computed(() => ({
  r2: r.value.fit?.r2_log?.toFixed(2) ?? '—',
  even: fmtW(r.value.fit?.halves?.even_days?.w_log),
  odd: fmtW(r.value.fit?.halves?.odd_days?.w_log),
  crossings: r.value.crossings ?? 0,
}))
</script>

<template>
  <section class="load" :class="[`cat-${r.category}`, { compact }]">
    <div class="words">
      <div class="label">
        {{ t('datasets.load.title', { h: r.window_hours ?? 168 }) }}
        <InfoHint>{{ t('datasets.load.hint') }}</InfoHint>
        <span class="badge">{{ t(`datasets.load.cat.${r.category}`) }}</span>
      </div>
      <p v-if="!compact" class="sentence">{{ sentence }}</p>

      <div v-if="hasScale" class="scale" :class="{ faded: r.category === 'unclear' }">
        <div class="track">
          <span class="z idle" :style="{ width: pct(idleBelow) }" />
          <span class="z moderate" :style="{ width: pct(busyFrom - idleBelow) }" />
          <span class="z busy" />
          <span class="mark" :style="{ left: pct(w) }" :title="`${share}`" />
        </div>
        <div class="ticks">
          <span>{{ t('datasets.load.cat.idle') }}</span>
          <span :style="{ left: pct((idleBelow + busyFrom) / 2) }" class="mid">{{ t('datasets.load.cat.moderate') }}</span>
          <span class="end">{{ t('datasets.load.cat.busy') }}</span>
        </div>
      </div>

      <div v-if="fits && !compact" class="fits"><span class="fits-k">{{ t('datasets.load.fitsLabel') }}</span> {{ fits }}</div>

      <dl v-if="r.c_typ !== undefined && !compact && groups.length < 2" class="facts">
        <div><dt>{{ t('datasets.load.typical') }}</dt><dd>{{ r.c_typ.toFixed(1) }}</dd></div>
        <div><dt>{{ t('datasets.load.busiest') }}</dt><dd>{{ r.c_peak?.toFixed(1) ?? '—' }}</dd></div>
        <div><dt>{{ t('datasets.load.read') }}</dt><dd>{{ (r.requests ?? 0).toLocaleString() }}</dd></div>
      </dl>

      <el-popover v-if="r.fit" trigger="click" :width="360" placement="bottom-start">
        <template #reference>
          <button type="button" class="trust">{{ t('datasets.load.trust') }}</button>
        </template>
        <ul class="checks">
          <li v-for="[name, ok] in checks" :key="name">
            <span :class="ok === false ? (hard.includes(name) ? 'bad' : 'note') : ok ? 'ok' : 'na'">{{ ok === false ? (hard.includes(name) ? '✗' : '!') : ok ? '✓' : '–' }}</span>
            {{ t(`datasets.load.check.${name}`, checkArgs) }}
            <span v-if="ok === false && !hard.includes(name)" class="na">{{ t('datasets.load.noteOnly') }}</span>
          </li>
        </ul>
      </el-popover>
    </div>

    <div v-if="groups.length >= 2" class="charts" :class="{ faded: r.category === 'unclear' }">
      <figure class="chart">
        <figcaption>{{ t('datasets.load.slowdownAxis') }}
          <InfoHint :width="320">{{ t('datasets.load.slowdownHint') }}</InfoHint></figcaption>
        <svg :viewBox="`0 0 ${CW} ${H}`" role="img" :aria-label="t('datasets.load.slowdownAxis')">
          <line v-for="v in sTicks" :key="`g${v}`" :x1="L" :x2="CW - R" :y1="sy(v)" :y2="sy(v)" class="grid" />
          <text v-for="v in sTicks" :key="`t${v}`" :x="L - 4" :y="sy(v) + 3" text-anchor="end" class="tick">{{ v }}×</text>
          <g v-if="kneeIndex !== null">
            <line :x1="kneeX" :x2="kneeX" :y1="PT - 12" :y2="BASE" class="knee" />
            <text :x="kneeLabel.x" :y="PT - 4" :text-anchor="kneeLabel.anchor" class="knee-label">{{ t('datasets.load.knee') }}</text>
          </g>
          <polyline :points="line" class="curve" />
          <g v-for="(g, i) in groups" :key="`p${g.group}`">
            <circle :cx="x(i)" :cy="sy(g.slowdown)" r="3.5" class="dot" />
            <text :x="x(i)" :y="BASE + 12" text-anchor="middle" class="tick">{{ tickLabel(g, i) }}</text>
            <rect :x="x(i) - band / 2" :y="PT" :width="band" :height="PH" fill="transparent"><title>{{ tip(g) }}</title></rect>
          </g>
          <line :x1="L" :x2="CW - R" :y1="BASE" :y2="BASE" class="baseline" />
          <text :x="(L + CW - R) / 2" :y="H - 2" text-anchor="middle" class="axis-title">{{ t('datasets.load.xAxis') }}</text>
        </svg>
      </figure>

      <figure class="chart">
        <figcaption>{{ t('datasets.load.requestsAxis') }}</figcaption>
        <svg :viewBox="`0 0 ${CW} ${H}`" role="img" :aria-label="t('datasets.load.requestsAxis')">
          <g v-if="kneeIndex !== null">
            <line :x1="kneeX" :x2="kneeX" :y1="PT - 12" :y2="BASE" class="knee" />
            <text :x="kneeLabel.x" :y="PT - 4" :text-anchor="kneeLabel.anchor" class="knee-label">{{ t('datasets.load.pastKnee', { share }) }}</text>
          </g>
          <g v-for="(g, i) in groups" :key="`b${g.group}`">
            <rect :x="x(i) - barW / 2" :y="by(g.requests)" :width="barW" :height="Math.max(1, BASE - by(g.requests))"
              rx="2" :class="past(g) ? 'bar past' : 'bar'"><title>{{ tip(g) }}</title></rect>
            <text :x="x(i)" :y="BASE + 12" text-anchor="middle" class="tick">{{ tickLabel(g, i) }}</text>
          </g>
          <line :x1="L" :x2="CW - R" :y1="BASE" :y2="BASE" class="baseline" />
          <g v-for="m in marks" :key="m.cls">
            <line :x1="m.x" :x2="m.x" :y1="PT" :y2="BASE + 2" class="mark-line" />
            <path :d="`M${m.x - 4},${MARK_Y - 10 + m.row * 12} l4,-5 l4,5 z`" class="mark-tri" />
            <text :x="m.x" :y="MARK_Y + 1 + m.row * 12" :text-anchor="markAnchor(m.x)" class="mark-label">{{ m.text }}</text>
          </g>
          <text :x="(L + CW - R) / 2" :y="H - 2" text-anchor="middle" class="axis-title">{{ t('datasets.load.xAxis') }}</text>
        </svg>
      </figure>
    </div>
  </section>
</template>

<style scoped>
.load {
  display: grid; grid-template-columns: minmax(260px, 1fr) minmax(300px, 1.3fr); gap: 8px 24px;
  padding: 14px 18px; border-top: 1px solid var(--autotune-border);
}
.label {
  display: flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.04em; color: var(--autotune-muted);
}
.badge {
  margin-left: 4px; padding: 1px 8px; border-radius: 10px; font-size: 11px; letter-spacing: 0.02em;
  background: var(--el-fill-color-light); color: var(--autotune-text);
}
.cat-idle .badge { background: var(--el-color-primary-light-9); color: var(--el-color-primary-dark-2); }
.cat-moderate .badge { background: var(--el-color-primary-light-7); color: var(--el-color-primary-dark-2); }
.cat-busy .badge { background: var(--el-color-primary); color: #fff; }
.cat-unclear .badge { background: transparent; box-shadow: inset 0 0 0 1px var(--autotune-border); color: var(--autotune-muted); }
.sentence { margin: 8px 0 10px; font-size: 13.5px; line-height: 1.45; color: var(--autotune-text); }

.scale { max-width: 340px; }
.track { position: relative; display: flex; height: 10px; border-radius: 5px; overflow: visible; }
.z { display: block; height: 100%; }
.z.idle { background: var(--el-color-primary-light-9); border-radius: 5px 0 0 5px; }
.z.moderate { background: var(--el-color-primary-light-5); }
.z.busy { flex: 1; background: var(--el-color-primary); border-radius: 0 5px 5px 0; }
.mark {
  position: absolute; top: -4px; width: 3px; height: 18px; margin-left: -1.5px; border-radius: 2px;
  background: var(--autotune-text); box-shadow: 0 0 0 2px var(--autotune-surface);
}
.ticks { position: relative; height: 16px; margin-top: 3px; font-size: 11px; color: var(--autotune-muted); }
.ticks span { position: absolute; left: 0; }
.ticks .mid { transform: translateX(-50%); }
.ticks .end { left: auto; right: 0; }

.fits { margin-top: 8px; font-size: 13px; color: var(--autotune-text); }
.fits-k { color: var(--autotune-muted); }
.facts { display: flex; flex-wrap: wrap; gap: 4px 20px; margin: 10px 0 4px; }
.facts dt { font-size: 11.5px; color: var(--autotune-muted); }
.facts dd { margin: 0; font-size: 14px; font-weight: 600; color: var(--autotune-text); font-variant-numeric: tabular-nums; }
.trust {
  padding: 0; border: none; background: none; cursor: pointer; font-size: 12px;
  color: var(--el-color-primary); text-decoration: underline dotted;
}
.checks { margin: 0; padding: 0; list-style: none; font-size: 12.5px; line-height: 1.6; }
.checks .ok { color: var(--el-color-success-dark-2); }
.checks .bad { color: var(--el-color-danger-dark-2); }
.checks .na { color: var(--el-text-color-placeholder); }
.checks .note { color: var(--el-color-warning-dark-2); font-weight: 600; }
.faded { opacity: 0.5; }

.charts { display: flex; flex-wrap: wrap; gap: 8px 20px; }
.chart { flex: 1 1 260px; max-width: 420px; margin: 0; }
.chart figcaption { font-size: 12px; color: var(--autotune-text); margin-bottom: 2px; display: flex; align-items: center; gap: 4px; }
.chart svg { width: 100%; height: auto; display: block; }
.axis-title { font-size: 10px; fill: var(--autotune-muted); }
.tick { font-size: 9px; fill: var(--autotune-muted); }
.grid { stroke: var(--el-border-color-lighter); stroke-width: 1; }
.baseline { stroke: var(--el-border-color); stroke-width: 1; }
.curve { fill: none; stroke: var(--el-color-primary); stroke-width: 2; stroke-linejoin: round; }
.dot { fill: var(--el-color-primary); stroke: var(--autotune-surface); stroke-width: 2; }
.bar { fill: var(--el-border-color-darker); }
.bar.past { fill: var(--el-color-primary); }
.knee { stroke: var(--autotune-text); stroke-width: 1; stroke-dasharray: 3 3; }
.knee-label { font-size: 10px; fill: var(--autotune-text); }

.mark-line { stroke: var(--autotune-muted); stroke-width: 1; stroke-dasharray: 1 3; opacity: 0.7; }
.mark-tri { fill: var(--autotune-text); }
.mark-label { font-size: 10px; fill: var(--autotune-text); font-weight: 600; }

/* In a form the panel is one boxed block: header and scale on top, the chart
   under it, nothing repeated in words. */
.load.compact {
  grid-template-columns: 1fr; gap: 6px; padding: 0; border-top: none;
}
.load.compact .words { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 18px; }
.load.compact .scale { flex: 1 1 220px; max-width: 320px; }

@media (max-width: 1000px) {
  .load { grid-template-columns: 1fr; }
}
</style>
