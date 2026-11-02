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
 *
 *  The bars are the whole board: hovering one shows its exact config (what it
 *  changes from production first), clicking one asks the page to open its
 *  launch config and commands. Production measured several times (anchors,
 *  each night's validation) is one bar, the mean the percentages are taken
 *  against, not a row per measurement.
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
  breaches?: string[]
  card_type?: string
  comparable?: boolean
}

/** A bar: one row, or production's measurements merged into their mean. */
export interface ShownRow extends ScoreRow {
  /** How many baseline measurements this bar averages (1 for an ordinary row). */
  n: number
  values: number[]
  run_ids: number[]
}

const props = defineProps<{
  rows: ScoreRow[]
  /** Which config keys actually vary — the rest is identical on every row. */
  sweptKeys: string[]
  /** "minimize" flips which end is best. */
  direction?: string
  /** Production's config: the hover marks what each config changes from it. */
  production?: Record<string, unknown> | null
  /** The objective's unit, for the hover. */
  unit?: string
}>()

const emit = defineEmits<{ select: [row: ShownRow] }>()

const scored = computed(() => props.rows.filter((r) => r.score !== null))

/** The bars in board order, the baseline's measurements collapsed into one
 *  bar placed by their mean: where the best of them ranked would put a bar
 *  among configs that beat it. */
const shown = computed<ShownRow[]>(() => {
  const base = scored.value.filter((r) => r.is_baseline)
  const out: ShownRow[] = scored.value
    .filter((r) => !r.is_baseline)
    .map((r) => ({ ...r, n: 1, values: [r.score!], run_ids: [r.run_id] }))
  if (!base.length) return out
  const values = base.map((b) => b.score!)
  const bar: ShownRow = {
    ...base[0],
    score: values.reduce((a, b) => a + b, 0) / values.length,
    holds_redlines: base.every((b) => b.holds_redlines),
    n: values.length,
    values,
    run_ids: base.map((b) => b.run_id),
  }
  const beats = (r: ShownRow) =>
    r.holds_redlines && (props.direction === 'minimize' ? r.score! < bar.score! : r.score! > bar.score!)
  const at = out.findIndex((r) => !beats(r))
  out.splice(at < 0 ? out.length : at, 0, bar)
  return out
})

/** Bars are absolute, so the scale is the largest value present — including a
 *  rejected one, which still happened and still has a size. */
const scale = computed(() => Math.max(...shown.value.map((r) => Math.abs(r.score!)), 1))

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

/** The card production was measured on: a bar from another card is not on its
 *  per-card scale, so it carries no percentage and the hover says why. */
const baselineCard = computed(() => scored.value.find((r) => r.is_baseline && r.card_type)?.card_type ?? '')
const offCard = (row: ScoreRow) =>
  !row.is_baseline && !!row.card_type && !!baselineCard.value && row.card_type !== baselineCard.value

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

const isSet = (config: Record<string, unknown>, k: string) =>
  config[k] !== undefined && config[k] !== null

/** Without a declared sweep (a policy's configs), the keys whose values
 *  differ between the bars: what every config sets alike says nothing about
 *  which bar is which. */
const varying = computed(() => {
  const rows = scored.value.filter((r) => !r.is_baseline && !r.config.__baseline__)
  const keys = new Set(rows.flatMap((r) => Object.keys(r.config).filter((k) => !k.startsWith('__'))))
  return [...keys].filter((k) => new Set(rows.map((r) => JSON.stringify(r.config[k]))).size > 1).sort()
})

/** The keys the labels name: the declared sweep, else what varies. */
const keysShown = computed(() => (props.sweptKeys.length ? props.sweptKeys : varying.value))

/** Every bar sets every swept key (a grid): values alone read cleanly under
 *  one caption naming the keys. A policy's configs each set a few keys of
 *  many, and "0.9" alone could be anyone's: those bars name their keys. */
const dense = computed(() =>
  keysShown.value.length > 0 && scored.value.every((r) =>
    r.is_baseline || r.config.__baseline__ || keysShown.value.every((k) => isSet(r.config, k))))

function summary(config: Record<string, unknown>): string {
  if (config.__baseline__) return 'baseline'
  const keys = keysShown.value
  const set = keys.filter((k) => isSet(config, k))
  if (!set.length) return 'defaults'
  return set.map((k) => (dense.value ? String(config[k]) : `${k}=${config[k]}`)).join(' · ')
}

function fmt(value: number): string {
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 })
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/** The config as the hover lists it: what it changes from production first
 *  (with production's value), then everything it shares with production. */
function configLines(config: Record<string, unknown>) {
  const prod = props.production ?? {}
  const keys = Object.keys(config).filter((k) => !k.startsWith('__')).sort()
  const changed = keys
    .filter((k) => !props.production || !same(config[k], prod[k]))
    .map((k) => ({ key: k, value: config[k], was: k in prod ? prod[k] : undefined, inProd: k in prod }))
  const removed = Object.keys(prod).filter((k) => !(k in config) && !k.startsWith('__')).sort()
  const kept = props.production ? keys.filter((k) => same(config[k], prod[k])) : []
  return { changed, removed, kept }
}

const show = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v))
</script>

<template>
  <div v-if="scored.length" class="bars">
    <div class="caption muted tiny">
      <!-- Each bar is labelled with values only; say once whose they are. The
           metric is named once, by the board above; the bars start at zero. -->
      <span v-if="dense" class="mono">{{ keysShown.join(' · ') }}</span>
      <template v-if="hasBaseline">
        <span v-if="dense" class="sep">·</span>
        <span class="legend">
          <span class="chip k-better"><i class="dot" />better</span>
          <span class="chip k-baseline"><i class="dot" />baseline</span>
          <span class="chip k-worse"><i class="dot" />worse</span>
          <span v-if="hasRejected" class="chip k-rejected"><i class="dot" />rejected</span>
        </span>
      </template>
    </div>
    <div class="rows" :class="{ scroll: shown.length > 14 }">
      <!-- Below the row, or above it near the bottom of the window: a row is
           as wide as the card, so there is no room beside it. -->
      <el-popover v-for="row in shown" :key="row.run_id" placement="bottom-start" trigger="hover"
        :fallback-placements="['top-start', 'bottom-start']" :width="400" :show-after="120" :hide-after="60" :persistent="false" popper-class="bar-pop">
        <template #reference>
          <div class="row" :class="'k-' + kindOf(row)" role="button" tabindex="0"
            @click="emit('select', row)" @keydown.enter="emit('select', row)">
            <span class="label mono">
              <i class="dot" />
              <span class="txt">{{ summary(row.config) }}</span>
              <span v-if="row.is_baseline" class="tag">baseline{{ row.n > 1 ? ` · mean of ${row.n}` : '' }}</span>
            </span>
            <span class="track">
              <span class="fill" :style="{ width: (Math.abs(row.score!) / scale) * 100 + '%' }" />
            </span>
            <span class="value mono">{{ fmt(row.score!) }}</span>
            <span class="delta mono">{{ pct(row) }}</span>
          </div>
        </template>
        <div class="pop">
          <div class="pop-head">
            <b class="mono">{{ fmt(row.score!) }}</b>
            <span v-if="unit" class="muted">{{ unit }}</span>
            <span v-if="pct(row)" class="mono" :class="'k-' + kindOf(row)">{{ pct(row) }} vs baseline</span>
            <span class="muted pop-run">
              {{ row.n > 1 ? `baseline · mean of ${row.n} runs` : (row.is_baseline ? 'baseline' : `run #${row.run_id}`) }}
            </span>
          </div>
          <div v-if="row.n > 1" class="muted tiny">runs {{ row.run_ids.map((i) => '#' + i).join(' ') }}: {{ row.values.map(fmt).join(' · ') }}</div>
          <div v-if="!row.holds_redlines" class="pop-bad tiny">Rejected: {{ (row.breaches ?? []).join('; ') || 'crossed a redline' }}</div>
          <div v-if="row.comparable === false" class="pop-bad tiny">Replayed a different dataset build than the campaign pinned.</div>
          <div v-if="offCard(row)" class="pop-bad tiny">
            Ran on {{ row.card_type }}, the baseline on {{ baselineCard }}: not comparable per card.</div>
          <div v-else-if="row.card_type" class="muted tiny">GPU {{ row.card_type }}</div>
          <template v-for="lines in [configLines(row.config)]" :key="'l'">
            <div v-if="production && !row.is_baseline" class="pop-sec tiny muted">
              {{ lines.changed.length || lines.removed.length ? 'Changed from baseline' : 'Same config as the baseline' }}</div>
            <div v-for="c in lines.changed" :key="c.key" class="kv mono" :class="{ changed: production && !row.is_baseline }">
              <span>{{ c.key }}</span><span>{{ show(c.value) }}<i v-if="production && !row.is_baseline" class="was">
                {{ c.inProd ? `was ${show(c.was)}` : 'not set in the baseline' }}</i></span>
            </div>
            <div v-for="k in lines.removed" :key="'-' + k" class="kv mono changed">
              <span>{{ k }}</span><span><i class="was">unset (baseline: {{ show(production?.[k]) }})</i></span>
            </div>
            <template v-if="lines.kept.length">
              <div class="pop-sec tiny muted">{{ row.is_baseline ? 'Config' : 'Same as the baseline' }}</div>
              <div v-for="k in lines.kept" :key="'=' + k" class="kv mono muted">
                <span>{{ k }}</span><span>{{ show(row.config[k]) }}</span>
              </div>
            </template>
          </template>
          <div class="pop-foot tiny muted">Click for the launch config and commands</div>
        </div>
      </el-popover>
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
  cursor: pointer;
  border-radius: 3px;
  display: grid;
  grid-template-columns: minmax(120px, 360px) 1fr 92px 58px;
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
  background: var(--el-color-success);
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
  background: var(--el-color-success);
  border-radius: 3px;
}
.row:hover,
.row:focus-visible {
  background: var(--el-fill-color-light, #f5f7fa);
  outline: none;
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
.pop {
  font-size: 12px;
  line-height: 1.5;
  /* A long config scrolls rather than running off the window. */
  max-height: min(60vh, 520px);
  overflow-y: auto;
}
.pop-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
  margin-bottom: 4px;
}
.pop-run {
  margin-left: auto;
}
.pop-head .k-better {
  color: var(--el-color-success);
}
.pop-head .k-worse {
  color: var(--el-color-warning);
}
.pop-bad {
  color: var(--el-color-danger);
}
.pop-sec {
  margin-top: 6px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  font-size: 10px;
}
.kv {
  display: grid;
  grid-template-columns: minmax(120px, auto) 1fr;
  gap: 8px;
  font-size: 11.5px;
  word-break: break-all;
}
.kv.changed {
  font-weight: 600;
}
.kv .was {
  font-style: normal;
  font-weight: 400;
  color: var(--autotune-muted);
  margin-left: 6px;
}
.pop-foot {
  margin-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter, #ebeef5);
  padding-top: 4px;
}
</style>
