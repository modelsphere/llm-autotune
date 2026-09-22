<script setup lang="ts">
/** A leaderboard as bars — the shape of a night, rather than 48 table rows.
 *
 *  Bars are drawn from zero, not from the lowest score. Scaling to the range
 *  would make a 3% spread look like a landslide, which is the opposite of what
 *  this platform is for: the whole reason confirmation runs exist is that small
 *  differences on this rig are usually noise. The percentage beside each bar
 *  carries the ordering precisely, so nothing is lost by being honest about
 *  magnitude.
 *
 *  Colour carries the one comparison an operator actually asks of a night: is
 *  this config better or worse than what production runs today? When a baseline
 *  was measured this stage, every bar is tinted by which side of it it landed —
 *  green better, amber worse, slate the baseline itself — with a redline breach
 *  overriding all three in red, because a rejected config is rejected however
 *  fast it was.
 */
import { computed } from 'vue'

import { beatsBaselineRatio, fmtBaselineDelta } from '../utils/baseline'

export interface ScoreRow {
  run_id: number
  config: Record<string, unknown>
  score: number | null
  holds_redlines: boolean
  /** True for the relaunched production baseline this stage ranks against. */
  is_baseline?: boolean
  /** This score ÷ the same-stage baseline. Null when no baseline ran. */
  vs_baseline?: number | null
}

const props = defineProps<{
  rows: ScoreRow[]
  /** Which config keys actually vary — the rest is identical on every row. */
  sweptKeys: string[]
  /** What the number is, for the axis caption. */
  label: string
  unit?: string
  /** "minimize" flips which end is best. */
  direction?: string
}>()

const scored = computed(() => props.rows.filter((r) => r.score !== null))

/** Bars are absolute, so the scale is the largest value present — including a
 *  rejected one, which still happened and still has a size. */
const scale = computed(() => Math.max(...scored.value.map((r) => Math.abs(r.score!)), 1))

/** The best score, direction-aware: for a latency objective that is the lowest.
 *  Taken from the entries that HOLD their redlines — a rejected config is not
 *  the bar everything else should be measured against. */
const best = computed(() => {
  const eligible = scored.value.filter((r) => r.holds_redlines)
  const pool = (eligible.length ? eligible : scored.value).map((r) => r.score!)
  return props.direction === 'minimize' ? Math.min(...pool) : Math.max(...pool)
})

/** Only worth a legend once a baseline actually ran this stage — otherwise
 *  there is no better/worse to explain and every bar is the neutral colour. */
const hasBaseline = computed(() => scored.value.some((r) => r.is_baseline))
const hasRejected = computed(() => scored.value.some((r) => !r.holds_redlines))

type Kind = 'rejected' | 'baseline' | 'better' | 'worse' | 'neutral'

/** The one classification that drives a row's colour. Redlines win: a breached
 *  config is red whatever it scored. Then the baseline is its own reference
 *  colour, and everything with a ratio is tinted by which side of it it fell. */
function kindOf(row: ScoreRow): Kind {
  if (!row.holds_redlines) return 'rejected'
  if (row.is_baseline) return 'baseline'
  if (row.vs_baseline != null) return beatsBaselineRatio(row.vs_baseline, props.direction) ? 'better' : 'worse'
  return 'neutral'
}

/** The number beside each bar: how it compares to the production baseline —
 *  "+2.0% / -6.2% vs prod" — the same signed percentage the leaderboard columns
 *  show. The baseline is the reference, so it carries no number. When no
 *  baseline ran this stage there is nothing to compare to, so fall back to the
 *  distance from the best eligible config just to keep the spread legible. */
function pct(row: ScoreRow): string {
  if (row.is_baseline) return ''
  if (row.vs_baseline != null) return fmtBaselineDelta(row.vs_baseline, props.direction)
  if (!best.value || row.score === best.value) return ''
  const raw = ((row.score! - best.value) / Math.abs(best.value)) * 100
  const delta = props.direction === 'minimize' ? -raw : raw
  if (Math.abs(delta) < 0.05) return '='
  return `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%`
}

function summary(config: Record<string, unknown>): string {
  if (config.__baseline__) return 'production'
  const keys = props.sweptKeys.length ? props.sweptKeys : Object.keys(config)
  return keys
    .filter((k) => config[k] !== undefined && config[k] !== null)
    .map((k) => String(config[k]))
    .join(' · ')
}

function fmt(value: number): string {
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 })
}
</script>

<template>
  <div v-if="scored.length" class="bars">
    <div class="caption muted tiny">
      {{ label }}<span v-if="unit"> ({{ unit }})</span>
      <span class="sep">·</span>
      {{ scored.length }} measured, bars from zero
      <template v-if="hasBaseline">
        <span class="sep">·</span>
        <span class="legend">
          <span class="chip k-better"><i class="dot" />better</span>
          <span class="chip k-baseline"><i class="dot" />baseline</span>
          <span class="chip k-worse"><i class="dot" />worse</span>
          <span v-if="hasRejected" class="chip k-rejected"><i class="dot" />rejected</span>
        </span>
      </template>
    </div>
    <div class="rows" :class="{ scroll: scored.length > 14 }">
      <div v-for="row in scored" :key="row.run_id" class="row" :class="'k-' + kindOf(row)">
        <span class="label mono" :title="summary(row.config)">
          <i class="dot" />
          <span class="txt">{{ summary(row.config) }}</span>
          <span v-if="row.is_baseline" class="tag">baseline</span>
        </span>
        <span class="track">
          <span class="fill" :style="{ width: (Math.abs(row.score!) / scale) * 100 + '%' }" />
        </span>
        <span class="value mono">{{ fmt(row.score!) }}</span>
        <span class="delta mono">{{ pct(row) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bars {
  margin-bottom: 14px;
}
.caption {
  margin-bottom: 6px;
}
.sep {
  margin: 0 6px;
}
.legend {
  display: inline-flex;
  gap: 10px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.rows.scroll {
  max-height: 320px;
  overflow-y: auto;
  padding-right: 6px;
}
.row {
  display: grid;
  grid-template-columns: minmax(90px, 200px) 1fr 92px 58px;
  align-items: center;
  gap: 10px;
  height: 20px;
  font-size: 11.5px;
}
.label {
  display: flex;
  align-items: center;
  gap: 5px;
  overflow: hidden;
  color: var(--autotune-muted);
}
.label .txt {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.label .tag {
  flex: none;
  font-size: 9px;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  padding: 0 4px;
  border-radius: 3px;
  color: var(--el-color-info);
  background: var(--el-color-info-light-9, #f4f4f5);
  border: 1px solid var(--el-color-info-light-7, #d3d4d6);
}
/* A small swatch on every bar, echoing the legend, so a single row is legible
   without hunting for the caption. */
.dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--el-color-primary);
}
.track {
  background: var(--autotune-bg, #f5f7fa);
  border-radius: 3px;
  height: 10px;
  overflow: hidden;
}
.fill {
  display: block;
  height: 100%;
  background: var(--el-color-primary);
  border-radius: 3px;
}
.value {
  text-align: right;
}
.delta {
  text-align: right;
  color: var(--autotune-muted);
}

/* The three-way baseline comparison, plus the redline override. Each kind
   colours its swatch, its bar fill, and (for the extremes) its number. */
.k-better .dot,
.k-better .fill {
  background: var(--el-color-success);
}
.k-baseline .dot,
.k-baseline .fill {
  background: var(--el-color-info);
}
.k-worse .dot,
.k-worse .fill {
  background: var(--el-color-warning);
}
/* A config that crossed a redline still ran and still has a size — greyed
   rather than hidden, so "fast but rejected" is visible as exactly that. */
.k-rejected .dot,
.k-rejected .fill {
  background: var(--el-color-danger-light-5, #fab6b6);
}
.k-rejected .value {
  color: var(--el-color-danger);
}
</style>
