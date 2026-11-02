<script setup lang="ts">
/** One replay dataset as a card: what it collects (header), the sample in use
 *  (size, prompt lengths, cache hits), when the next one comes and what holds
 *  it back, the last few collections, and the campaigns that replay it. */
import {
  Clock, Coin, DataAnalysis, Loading, Lock, MoreFilled, Refresh,
} from '@element-plus/icons-vue'
import { computed } from 'vue'
import type { DatasetBuildState, ReplayDataset } from '../api/client'
import { useI18n } from '../i18n'
import { absoluteTime, exactTime, relativeTime } from '../utils/time'
import CampaignChips from './CampaignChips.vue'
import InfoHint from './InfoHint.vue'

const props = defineProps<{
  dataset: ReplayDataset
  /** Its recent collections, newest first; null while they load. */
  builds: DatasetBuildState[] | null
  collector: boolean
  llmbenchUrl: string
}>()
const emit = defineEmits<{ collect: []; edit: []; remove: []; llmbench: [] }>()

const { t } = useI18n()
const d = computed(() => props.dataset)
const fmt = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString())
/** 140,213 → "140K": token counts read at a glance. */
function short(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}K`
  return String(n)
}

const inFlight = computed(() => d.value.last_build?.status === 'pending' || d.value.last_build?.status === 'running')
/** The last attempt failed after the sample now in use was taken. */
const lastFailed = computed(() => {
  const b = d.value.last_build
  if (b?.status !== 'failed') return false
  if (!d.value.current) return true
  return (b.created_at ?? '') > (d.value.current.built_at ?? '')
})

const state = computed(() => {
  if (inFlight.value) return { key: 'collecting', label: t('datasets.card.collecting') }
  if (d.value.current && lastFailed.value) return { key: 'warn', label: t('datasets.card.readyWarn') }
  if (d.value.current) return { key: 'ready', label: t('datasets.card.ready') }
  if (lastFailed.value) return { key: 'failed', label: t('datasets.card.failed') }
  return { key: 'empty', label: t('datasets.card.empty') }
})

const nodes = computed(() => (d.value.forwarded_to.length ? d.value.forwarded_to.join(', ') : t('datasets.card.allNodes')))

const refreshLabel = computed(() => {
  const x = d.value
  if (x.external) return x.schedule_hours ? t('datasets.card.everyHOnLlmbench', { n: x.schedule_hours }) : t('datasets.card.onlyWhenAsked')
  if (!x.refresh_hours) return t('datasets.card.onlyWhenAsked')
  if (x.refresh_hours === 24) return t('datasets.card.everyDay')
  if (x.refresh_hours === 168) return t('datasets.card.everyWeek')
  return t('datasets.card.everyH', { n: x.refresh_hours })
})
const untilNext = computed(() => {
  if (!d.value.next_refresh_at) return ''
  const ms = new Date(d.value.next_refresh_at).getTime() - Date.now()
  if (ms <= 0) return t('datasets.card.dueNow')
  const hours = ms / 3_600_000
  if (hours < 1) return t('datasets.card.nextMin', { n: Math.ceil(ms / 60_000) })
  return hours < 48 ? t('datasets.card.nextH', { n: Math.round(hours) }) : t('datasets.card.nextDays', { n: Math.round(hours / 24) })
})

/** Prompt-length bands as columns, tallest = full height. */
const bands = computed(() => {
  const buckets = d.value.current?.buckets ?? {}
  const entries = Object.entries(buckets)
  const top = Math.max(1, ...entries.map(([, n]) => n))
  const total = entries.reduce((a, [, n]) => a + n, 0) || 1
  return entries.map(([band, count]) => ({ band, count, height: count / top, share: count / total }))
})

const summary = computed(() => d.value.current?.summary ?? null)
const cacheHit = computed(() => summary.value?.cache_hit_rate ?? null)
/** The donut's arc, as a dash on a circle of circumference 2πr (r = 15). */
const ARC = 2 * Math.PI * 15

/** The last few collections, oldest first, for the strip. */
const history = computed(() => {
  const list = (props.builds ?? []).slice(0, 12).reverse()
  const top = Math.max(1, ...list.map((b) => b.records ?? 0))
  return list.map((b) => ({
    ...b,
    height: b.status === 'ready' ? Math.max(0.12, (b.records ?? 0) / top) : b.status === 'failed' ? 1 : 0.5,
    tip: `${absoluteTime(b.created_at)} · ${b.status}${b.records != null ? ` · ${fmt(b.records)} ${t('datasets.card.requestsWord')}` : ''}${b.error ? ` — ${b.error}` : ''}`,
  }))
})

const holding = computed(() => d.value.held_by.map((c) => c.id))
/** Holders are campaigns too, even when the API lists them only as holders. */
const campaigns = computed(() => {
  const seen = new Map(d.value.used_by.map((c) => [c.id, c]))
  for (const c of d.value.held_by) if (!seen.has(c.id)) seen.set(c.id, c)
  return [...seen.values()]
})

function onCommand(command: string) {
  if (command === 'delete') emit('remove')
  if (command === 'llmbench') emit('llmbench')
}
</script>

<template>
  <article class="card" :class="`st-${state.key}`">
    <header class="head">
      <span class="avatar"><el-icon :size="18"><Coin /></el-icon></span>
      <div class="ident">
        <div class="name-row">
          <span class="name">{{ d.display_name || d.name }}</span>
          <span class="pill" :class="`pill-${state.key}`">
            <el-icon v-if="state.key === 'collecting'" class="is-loading"><Loading /></el-icon>
            <span v-else class="pill-dot" />
            {{ state.label }}
          </span>
          <el-tag v-if="d.external" size="small" effect="plain" type="info"
            :title="t('datasets.card.onLlmbenchTip')">{{ t('datasets.card.onLlmbench') }}</el-tag>
        </div>
        <div class="meta">
          <span>{{ t('datasets.card.model') }} <b>{{ d.model || '—' }}</b></span>
          <span class="sep">·</span>
          <span :class="{ mono: d.forwarded_to.length }">{{ nodes }}</span>
          <span class="sep">·</span>
          <span>{{ t('datasets.card.fromLast', { n: fmt(d.sample_size), h: d.window_hours }) }}</span>
          <span class="sep">·</span>
          <span class="mono id">{{ d.name }}</span>
        </div>
      </div>
      <div class="actions">
        <el-button size="small" :icon="Refresh" :disabled="inFlight || !collector"
          @click="emit('collect')">{{ t('datasets.card.collectNow') }}</el-button>
        <el-button v-if="d.can_edit" size="small" @click="emit('edit')">{{ t('datasets.card.edit') }}</el-button>
        <el-dropdown v-if="d.can_edit || llmbenchUrl" trigger="click" @command="onCommand">
          <el-button size="small" class="icon-btn" :aria-label="t('datasets.card.more')"><el-icon><MoreFilled /></el-icon></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="llmbenchUrl" command="llmbench">{{ t('datasets.card.openOnLlmbench') }}</el-dropdown-item>
              <el-dropdown-item v-if="d.can_edit" command="delete" divided>
                <span class="danger">{{ t('datasets.card.delete') }}</span></el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <div v-if="inFlight" class="progress">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span class="clip" :title="d.last_build?.progress ?? ''">{{ d.last_build?.progress || t('datasets.card.queued') }}</span>
    </div>
    <div v-else-if="lastFailed" class="failure" :title="d.last_build?.error ?? ''">
      {{ d.current ? t('datasets.card.failedKept') : t('datasets.card.failedPrefix') }}
      {{ d.last_build?.error || t('datasets.card.noReason') }}
    </div>

    <div class="body">
      <!-- The sample in use -->
      <section class="cell sample">
        <div class="cell-label"><el-icon><DataAnalysis /></el-icon> {{ t('datasets.card.sampleInUse') }}</div>
        <template v-if="d.current">
          <div class="big">{{ fmt(d.current.records) }}<span class="unit">{{ t('datasets.card.requestsWord') }}</span></div>
          <div class="sub" :title="exactTime(d.current.built_at)">{{ t('datasets.card.taken', { ago: relativeTime(d.current.built_at) }) }}</div>
          <div v-if="d.current.window_start" class="sub tiny">
            {{ t('datasets.card.traffic') }} {{ absoluteTime(d.current.window_start) }} → {{ absoluteTime(d.current.window_end) }}</div>
          <div v-if="cacheHit != null" class="cache">
            <svg viewBox="0 0 36 36" class="donut" aria-hidden="true">
              <circle cx="18" cy="18" r="15" class="track" />
              <circle cx="18" cy="18" r="15" class="arc"
                :stroke-dasharray="`${ARC * cacheHit} ${ARC}`" transform="rotate(-90 18 18)" />
            </svg>
            <div>
              <div class="cache-val">{{ Math.round(cacheHit * 100) }}%</div>
              <div class="sub tiny">{{ t('datasets.card.cached') }}
                <InfoHint>{{ t('datasets.card.cachedHint') }}</InfoHint>
              </div>
            </div>
          </div>
        </template>
        <div v-else class="none">{{ inFlight ? t('datasets.card.firstOnWay') : t('datasets.card.noSample') }}</div>
      </section>

      <!-- What the requests look like -->
      <section class="cell shape">
        <div class="cell-label">
          {{ t('datasets.card.promptLengths') }}
          <InfoHint>{{ t('datasets.card.promptLengthsHint') }}</InfoHint>
        </div>
        <div v-if="bands.length" class="hist">
          <div v-for="b in bands" :key="b.band" class="col"
            :title="t('datasets.card.bandTip', { band: b.band, n: fmt(b.count), pct: Math.round(b.share * 100) })">
            <span class="col-bar">
              <span class="col-n">{{ b.count ? `${Math.round(b.share * 100)}%` : '' }}</span>
              <span class="col-fill" :style="{ height: `${Math.max(b.height * 100, b.count ? 3 : 0)}%` }" />
            </span>
            <span class="col-label">{{ b.band }}</span>
          </div>
        </div>
        <div v-else class="none">—</div>
        <div v-if="summary?.prompt_tokens || summary?.completion_tokens" class="stats">
          <span v-if="summary?.prompt_tokens" :title="`${t('datasets.card.median')} ${fmt(summary.prompt_tokens.p50)} · p90 ${fmt(summary.prompt_tokens.p90)} · ${t('datasets.card.longest')} ${fmt(summary.prompt_tokens.max)}`">
            {{ t('datasets.card.prompt') }} <b>{{ short(summary.prompt_tokens.p50) }}</b> {{ t('datasets.card.median') }} · <b>{{ short(summary.prompt_tokens.p90) }}</b> p90</span>
          <span v-if="summary?.completion_tokens" :title="`${t('datasets.card.median')} ${fmt(summary.completion_tokens.p50)} · p90 ${fmt(summary.completion_tokens.p90)}`">
            {{ t('datasets.card.output') }} <b>{{ short(summary.completion_tokens.p50) }}</b> {{ t('datasets.card.median') }} · <b>{{ short(summary.completion_tokens.p90) }}</b> p90</span>
        </div>
      </section>

      <!-- When the next one comes -->
      <section class="cell next">
        <div class="cell-label"><el-icon><Clock /></el-icon> {{ t('datasets.card.nextSample') }}</div>
        <div class="next-line"><el-icon class="next-icon"><Refresh /></el-icon>{{ refreshLabel }}</div>
        <div v-if="d.held_by.length" class="held">
          <el-icon><Lock /></el-icon>
          <span>{{ d.held_by.length === 1 ? t('datasets.card.waitsForOne') : t('datasets.card.waitsFor', { n: d.held_by.length }) }}</span>
        </div>
        <div v-else-if="untilNext && !inFlight" class="sub">{{ untilNext }}</div>
        <div class="cell-label strip-label">{{ t('datasets.card.recent') }}</div>
        <div v-if="history.length" class="strip">
          <span v-for="b in history" :key="b.id" class="tick" :class="`b-${b.status}`" :title="b.tip">
            <span :style="{ height: `${b.height * 100}%` }" />
          </span>
        </div>
        <div v-else class="sub tiny">{{ builds ? t('datasets.card.noneYet') : t('datasets.card.loading') }}</div>
      </section>
    </div>

    <footer class="foot">
      <div class="used">
        <span class="foot-label">{{ campaigns.length === 1 ? t('datasets.card.replayedByOne') : campaigns.length ? t('datasets.card.replayedBy', { n: campaigns.length }) : t('datasets.card.noneReplays') }}</span>
        <CampaignChips v-if="campaigns.length" :campaigns="campaigns" :holding="holding" :max="5" />
      </div>
      <div class="source tiny">
        {{ t('datasets.card.fromSource') }} <span v-if="d.source_url" class="mono">{{ d.source_url }}</span><template v-else>{{ t('datasets.card.installStore') }}</template>
        · {{ t('datasets.card.status') }} {{ d.statuses.join(', ') || t('datasets.card.any') }} · {{ d.uris.join(', ') || t('datasets.card.anyPath') }}
        <template v-if="d.extra_logsql"> · <span class="mono">{{ d.extra_logsql }}</span></template>
      </div>
    </footer>
  </article>
</template>

<style scoped>
.card {
  background: var(--autotune-surface); border: 1px solid var(--autotune-border); border-radius: 10px;
  overflow: hidden;
}
.head { display: flex; gap: 12px; align-items: flex-start; padding: 14px 18px 12px; }
.avatar {
  flex: none; width: 34px; height: 34px; border-radius: 8px; display: inline-flex; align-items: center;
  justify-content: center; color: var(--el-color-primary); background: var(--el-color-primary-light-9);
}
.ident { flex: 1; min-width: 0; }
.name-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.name { font-size: 15px; font-weight: 600; color: var(--autotune-text); }
.meta { margin-top: 3px; font-size: 12.5px; color: var(--autotune-muted); display: flex; flex-wrap: wrap; gap: 0 6px; }
.meta b { font-weight: 500; color: var(--el-text-color-regular); }
.sep { color: var(--el-text-color-placeholder); }
.id { color: var(--el-text-color-placeholder); }
.actions { flex: none; display: flex; align-items: center; gap: 6px; }
.actions :deep(.el-button + .el-button) { margin-left: 0; }
.icon-btn { padding: 5px 7px; }
.danger { color: var(--el-color-danger); }

.pill {
  display: inline-flex; align-items: center; gap: 5px; height: 22px; padding: 0 9px;
  font-size: 12px; font-weight: 500; border-radius: 11px;
}
.pill-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.pill-ready { color: var(--el-color-success-dark-2); background: var(--el-color-success-light-9); }
.pill-collecting { color: var(--el-color-primary-dark-2); background: var(--el-color-primary-light-9); }
.pill-warn { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); }
.pill-failed { color: var(--el-color-danger-dark-2); background: var(--el-color-danger-light-9); }
.pill-empty { color: var(--autotune-muted); background: var(--el-fill-color-light); }

.progress, .failure {
  margin: 0 18px 10px; padding: 7px 12px; border-radius: 6px; font-size: 12.5px;
  display: flex; align-items: center; gap: 8px;
}
.progress { color: var(--el-color-primary-dark-2); background: var(--el-color-primary-light-9); }
.failure {
  color: var(--el-color-danger-dark-2); background: var(--el-color-danger-light-9);
  display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.clip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.body {
  display: grid; grid-template-columns: minmax(200px, 1fr) minmax(280px, 1.5fr) minmax(220px, 1fr);
  border-top: 1px solid var(--autotune-border);
}
.cell { padding: 14px 18px; min-width: 0; }
.cell + .cell { border-left: 1px solid var(--autotune-border); }
.cell-label {
  display: flex; align-items: center; gap: 5px; font-size: 11.5px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.04em; color: var(--autotune-muted); margin-bottom: 8px;
}
.big { font-size: 24px; font-weight: 600; color: var(--autotune-text); line-height: 1.15; font-variant-numeric: tabular-nums; }
.unit { font-size: 13px; font-weight: 400; color: var(--autotune-muted); margin-left: 6px; }
.sub { font-size: 12.5px; color: var(--autotune-muted); margin-top: 2px; }
.tiny { font-size: 11.5px; }
.none { font-size: 13px; color: var(--el-text-color-placeholder); padding: 6px 0; }

.cache { display: flex; align-items: center; gap: 10px; margin-top: 12px; }
.donut { width: 38px; height: 38px; flex: none; }
.donut circle { fill: none; stroke-width: 5; }
.donut .track { stroke: var(--el-fill-color-light); }
.donut .arc { stroke: var(--el-color-primary-light-3); stroke-linecap: round; }
.cache-val { font-size: 15px; font-weight: 600; color: var(--autotune-text); }

.hist { display: flex; align-items: flex-end; gap: 6px; height: 104px; }
.col { flex: 1; min-width: 0; height: 100%; display: flex; flex-direction: column; align-items: center; gap: 3px; }
.col-n { font-size: 10.5px; color: var(--autotune-muted); line-height: 14px; font-variant-numeric: tabular-nums; }
.col-bar { flex: 1; width: 100%; max-width: 44px; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; }
.col-fill {
  display: block; width: 100%; border-radius: 4px 4px 0 0;
  background: linear-gradient(180deg, var(--el-color-primary-light-3), var(--el-color-primary));
}
.col-label {
  font-size: 10.5px; color: var(--autotune-muted); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; max-width: 100%; font-family: var(--el-font-family);
}
.stats { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12.5px; color: var(--autotune-muted); }
.stats b { font-weight: 600; color: var(--autotune-text); }

.next-line { display: flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 500; color: var(--autotune-text); }
.next-icon { color: var(--el-color-primary); }
.held {
  display: flex; align-items: flex-start; gap: 6px; margin-top: 6px; font-size: 12.5px; line-height: 1.4;
  color: var(--el-color-warning-dark-2);
}
.held .el-icon { margin-top: 2px; flex: none; }
.strip-label { margin-top: 16px; margin-bottom: 6px; }
.strip { display: flex; align-items: flex-end; gap: 4px; height: 32px; }
.tick { width: 14px; height: 100%; display: flex; align-items: flex-end; cursor: default; }
.tick span { display: block; width: 100%; border-radius: 2px; }
.b-ready span { background: var(--el-color-success-light-3); }
.b-failed span { background: var(--el-color-danger-light-3); height: 100% !important; opacity: 0.85; }
.b-pending span, .b-running span {
  background: repeating-linear-gradient(45deg, var(--el-color-primary-light-5) 0 3px, var(--el-color-primary-light-8) 3px 6px);
}

.foot {
  border-top: 1px solid var(--autotune-border); background: var(--autotune-bg);
  padding: 10px 18px 12px; display: flex; flex-direction: column; gap: 8px;
}
.used { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.foot-label { font-size: 12.5px; color: var(--autotune-muted); white-space: nowrap; }
.source { color: var(--el-text-color-placeholder); line-height: 1.5; }
.source .mono { color: var(--autotune-muted); }

@media (max-width: 1000px) {
  .body { grid-template-columns: 1fr; }
  .cell + .cell { border-left: none; border-top: 1px solid var(--autotune-border); }
  .head { flex-wrap: wrap; }
  .ident { flex-basis: calc(100% - 46px); }
  .actions { width: 100%; padding-left: 46px; }
}
</style>
