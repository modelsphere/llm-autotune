<script setup lang="ts">
/** What each candidate is measured with, said as a workload.
 *
 *  A first-time user does not know an LLMBench benchmark slug, or what a
 *  dataset profile is; they know what load they want. So this asks for that —
 *  synthetic prompts of some size, or a replay of recorded traffic — and the
 *  platform creates the matching benchmark on LLMBench when the campaign is
 *  created. Naming an existing benchmark stays possible, behind a link.
 */
import { computed, ref, watch } from 'vue'
import { api, type DatasetProfile, type FrozenDataset } from '../api/client'
import { specOf, type BenchmarkChoice, type Workload } from '../utils/workload'
import { useI18n } from '../i18n'
import DatasetDialog from './DatasetDialog.vue'
import InfoHint from './InfoHint.vue'
import LoadReadingPanel from './LoadReadingPanel.vue'
import { useFlash } from '../utils/flash'
import BFCLFields from './BFCLFields.vue'

const props = defineProps<{
  profiles: DatasetProfile[]
  /** Frozen datasets on LLMBench: files that never change. */
  frozen?: FrozenDataset[]
  benchmarks: BenchmarkChoice[]
  /** The second stage offers replay, BFCL or an existing benchmark. */
  targetMetric?: string
  noSweep?: boolean
  /** What the campaign's objective ranks by, so the recommendation can tell
   *  whether it is already followed. */
  objectiveKind?: 'latency' | 'throughput' | ''
}>()
const w = defineModel<Workload>({ required: true })
/** Take a fresh sample of a rolling dataset when the campaign starts. */
const rebuild = defineModel<boolean>('rebuild', { default: true })
const emit = defineEmits<{
  module: [name: string]
  /** A dataset was set up from here: the list of profiles is out of date. */
  'datasets-changed': []
  /** The user took the load reading's recommendation: rank by this. */
  recommend: [what: 'latency' | 'throughput']
}>()
const { t } = useI18n()
const newDatasetOpen = ref(false)
/** Naming an existing LLMBench benchmark is for people who already have one:
 *  offered behind a link rather than as a third mode beside the two loads. */
const showExisting = ref(w.value.mode === 'existing')
watch(() => w.value.mode, (mode) => { if (mode === 'existing') showExisting.value = true })
function useExisting(on: boolean) {
  showExisting.value = on
  w.value.mode = on ? 'existing' : 'replay'
}

function onDatasetSaved(name: string) {
  w.value.dataset_profile = name
  w.value.dataset_frozen = ''
  emit('datasets-changed')
}

/** One dropdown for both kinds: a rolling profile by its name, a frozen
 *  dataset as `frozen:<name>`. */
const FROZEN = 'frozen:'
const datasetChoice = computed({
  get: () => (w.value.dataset_frozen ? FROZEN + w.value.dataset_frozen : w.value.dataset_profile),
  set: (value: string) => {
    const frozen = value.startsWith(FROZEN)
    w.value.dataset_frozen = frozen ? value.slice(FROZEN.length) : ''
    w.value.dataset_profile = frozen ? '' : value
  },
})

function frozenNote(d: FrozenDataset): string {
  const size = d.records != null ? t('campaignNew.wl.requestsN', { n: d.records.toLocaleString() }) : ''
  const from = d.origin === 'upload' ? t('campaignNew.wl.imported') : t('campaignNew.wl.frozen')
  return [size, from].filter(Boolean).join(' · ')
}

/** What the workload becomes on LLMBench, from the API — so the page can
 *  name it and say whether it is new or reused. */
const resolved = ref<{ slug: string; name: string; module: string; exists: boolean | null } | null>(null)
let asked = 0
watch(
  [() => JSON.stringify(specOf(w.value)), () => props.targetMetric],
  async () => {
    const spec = specOf(w.value)
    const mine = ++asked
    if (!spec) {
      resolved.value = null
      const modules = props.benchmarks.find((b) => b.slug === w.value.slug)?.modules ?? []
      emit('module', modules[0]?.split('#')[0] ?? '')
      return
    }
    try {
      const { data } = await api.post('/benchmarks/spec', spec, { params: { target_metric: props.targetMetric } })
      if (mine !== asked) return
      resolved.value = data
      emit('module', data.module)
    } catch {
      if (mine === asked) resolved.value = null
    }
  },
  { immediate: true },
)
watch(() => w.value.slug, (slug) => {
  if (w.value.mode !== 'existing') return
  const modules = props.benchmarks.find((b) => b.slug === slug)?.modules ?? []
  emit('module', modules[0]?.split('#')[0] ?? '')
})

const chosenProfile = computed(
  () => props.profiles.find((p) => p.name === w.value.dataset_profile) ?? null)

/** A timed replay needs a timed dataset: a timed profile, or a frozen file
 *  (which may be one; LLMBench refuses it at the start if not). */
const timedOk = computed(() => !!w.value.dataset_frozen || chosenProfile.value?.layout === 'timed')
watch(timedOk, (ok) => { if (!ok && w.value.schedule === 'timed') w.value.schedule = 'closed' })

/** What the load reading recommends. Light traffic: Original load ranked by
 *  latency — requests rarely slow each other, so latency is what tuning
 *  changes. Moderate or busy: Fixed load ranked by throughput — requests
 *  queue, so capacity decides. Nothing when the reading cannot be trusted. */
const rec = computed(() => {
  const load = chosenProfile.value?.load
  if (!load || w.value.mode !== 'replay') return null
  const cat = load.category
  if (cat !== 'idle' && cat !== 'moderate' && cat !== 'busy') return null
  const timed = cat === 'idle'
  const schedule: 'timed' | 'closed' = timed ? 'timed' : 'closed'
  const objective: 'latency' | 'throughput' = timed ? 'latency' : 'throughput'
  return {
    cat,
    schedule,
    objective,
    needsDataset: timed && !timedOk.value,
    applied: w.value.schedule === schedule && props.objectiveKind === objective,
  }
})

const modeFlash = useFlash()
function applyRec() {
  if (!rec.value || rec.value.needsDataset) return
  w.value.schedule = rec.value.schedule
  modeFlash.fire()
  emit('recommend', rec.value.objective)
}

function profileNote(p: DatasetProfile): string {
  const size = p.records != null ? t('campaignNew.wl.requestsN', { n: p.records.toLocaleString() }) : t('campaignNew.wl.empty')
  const parts = [size]
  if (p.layout === 'timed') parts.push(t('campaignNew.wl.timedLayout'))
  if (p.load?.category && p.load.category !== 'unclear') parts.push(t(`datasets.load.cat.${p.load.category}`))
  if (!p.managed) parts.push(t('campaignNew.wl.resampled', { h: p.schedule_hours }))
  return parts.join(' · ')
}
</script>

<template>
  <div class="workload">
    <div v-if="!showExisting" class="modes-row">
      <el-radio-group v-model="w.mode" size="small" class="modes">
        <el-radio-button value="bfcl">BFCL V4</el-radio-button>
        <el-radio-button value="replay">{{ t('campaignNew.wl.replay') }}</el-radio-button>
        <el-radio-button v-if="!noSweep" value="sweep">{{ t('campaignNew.wl.sweep') }}</el-radio-button>
      </el-radio-group>
      <el-button link size="small" class="existing-link" @click="useExisting(true)">
        {{ t('campaignNew.wl.useExisting') }}</el-button>
    </div>
    <div v-else class="modes-row">
      <span class="muted tiny">{{ t('campaignNew.wl.existing') }}</span>
      <el-button link size="small" class="existing-link" @click="useExisting(false)">
        {{ t('campaignNew.wl.describe') }}</el-button>
    </div>

    <div v-if="w.mode === 'sweep'" class="grid">
      <label class="field">
        <span class="muted tiny">{{ t('campaignNew.wl.inTokens') }}
          <InfoHint>{{ t('campaignNew.wl.inTokensHint') }}</InfoHint></span>
        <el-input-number v-model="w.input_tokens" :min="1" :max="1000000" :step="512"
          size="small" controls-position="right" />
      </label>
      <label class="field">
        <span class="muted tiny">{{ t('campaignNew.wl.outTokens') }}
          <InfoHint>{{ t('campaignNew.wl.outTokensHint') }}</InfoHint></span>
        <el-input-number v-model="w.output_tokens" :min="1" :max="100000" :step="128"
          size="small" controls-position="right" />
      </label>
      <label class="field">
        <span class="muted tiny">{{ t('campaignNew.wl.concurrency') }}
          <InfoHint>{{ t('campaignNew.wl.concurrencyLevels') }}</InfoHint></span>
        <el-input v-model="w.concurrencies" size="small" class="mono" placeholder="1, 4, 16, 64" />
      </label>
      <label class="field">
        <span class="muted tiny">{{ t('campaignNew.wl.perSlot') }}
          <InfoHint :width="320">{{ t('campaignNew.wl.perSlotHint') }}</InfoHint></span>
        <el-input-number v-model="w.requests_per_concurrency" :min="1" :max="10000" :step="5"
          size="small" controls-position="right" />
      </label>
      <label class="field">
        <span class="muted tiny">{{ t('campaignNew.wl.maxS') }}
          <InfoHint>{{ t('campaignNew.wl.maxSHint') }}</InfoHint></span>
        <el-input-number v-model="w.max_seconds_per_level" :min="10" :max="7200" :step="60"
          size="small" controls-position="right" />
      </label>
    </div>

    <BFCLFields v-else-if="w.mode === 'bfcl'" v-model="w.bfcl" />

    <template v-else-if="w.mode === 'replay'">
      <label class="field">
        <span class="muted tiny">{{ t('campaignNew.wl.dataset') }}
          <InfoHint :width="380"><span v-html="t('campaignNew.wl.datasetHint')" /></InfoHint></span>
        <el-select v-model="datasetChoice" size="small" filterable
          :empty-values="[null, undefined]">
          <el-option value="" :label="t('campaignNew.wl.example')" />
          <el-option-group v-if="frozen?.length" :label="t('campaignNew.wl.rolling')">
            <el-option v-for="p in profiles" :key="p.name" :value="p.name"
              :label="p.display_name || p.name">
              <span>{{ p.display_name || p.name }}</span>
              <span class="muted opt-help">{{ profileNote(p) }}</span>
            </el-option>
          </el-option-group>
          <template v-else>
            <el-option v-for="p in profiles" :key="p.name" :value="p.name"
              :label="p.display_name || p.name">
              <span>{{ p.display_name || p.name }}</span>
              <span class="muted opt-help">{{ profileNote(p) }}</span>
            </el-option>
          </template>
          <el-option-group v-if="frozen?.length" :label="t('campaignNew.wl.frozenGroup')">
            <el-option v-for="d in frozen" :key="d.name" :value="FROZEN + d.name" :label="d.name">
              <span>{{ d.name }}</span>
              <span class="muted opt-help">{{ frozenNote(d) }}</span>
            </el-option>
          </el-option-group>
          <template #footer>
            <el-button link type="primary" size="small" @click="newDatasetOpen = true">
              {{ t('campaignNew.wl.newDataset') }}</el-button>
          </template>
        </el-select>
      </label>
      <DatasetDialog v-model="newDatasetOpen" @saved="onDatasetSaved" />
      <div v-if="chosenProfile?.load" class="reading">
        <LoadReadingPanel :reading="chosenProfile.load" compact />
        <div v-if="rec" class="rec" :class="{ applied: rec.applied, clickable: !rec.applied && !rec.needsDataset }"
          :role="!rec.applied && !rec.needsDataset ? 'button' : undefined"
          :tabindex="!rec.applied && !rec.needsDataset ? 0 : undefined"
          @click="!rec.applied && applyRec()" @keydown.enter="!rec.applied && applyRec()">
          <div class="rec-text">
            <div class="rec-title">
              {{ t('campaignNew.wl.rec.title', { cat: t(`datasets.load.cat.${rec.cat}`) }) }}:
              <b>{{ t(`campaignNew.wl.rec.${rec.schedule}`) }}</b>
            </div>
            <div class="rec-why">
              {{ rec.needsDataset ? t('campaignNew.wl.rec.needsDataset') : t(`campaignNew.wl.rec.why.${rec.cat}`) }}
            </div>
          </div>
          <span v-if="rec.applied" class="rec-done">✓ {{ t('campaignNew.wl.rec.applied') }}</span>
          <el-button v-else-if="rec.needsDataset" size="small" @click.stop="newDatasetOpen = true">
            {{ t('campaignNew.wl.rec.createDataset') }}</el-button>
          <el-button v-else size="small" type="primary" @click.stop="applyRec">
            {{ t('campaignNew.wl.rec.apply') }}</el-button>
        </div>
      </div>

      <div class="modes-row">
        <span class="row-label">{{ t('campaignNew.wl.schedule') }}
          <InfoHint :width="380">{{ t('campaignNew.wl.scheduleHint') }}</InfoHint></span>
        <span class="flashable" :class="{ flash: modeFlash.on.value }">
          <el-radio-group v-model="w.schedule" size="small">
            <el-radio-button value="closed">{{ t('campaignNew.wl.scheduleClosed') }}</el-radio-button>
            <el-radio-button value="timed" :disabled="!timedOk">{{ t('campaignNew.wl.scheduleTimed') }}</el-radio-button>
          </el-radio-group>
        </span>
        <span v-if="!timedOk" class="muted tiny">{{ t('campaignNew.wl.timedNeedsDataset') }}</span>
      </div>
      <div v-if="w.schedule !== 'timed'" class="grid">
        <label class="field">
          <span class="muted tiny">{{ t('campaignNew.wl.requests') }}
            <InfoHint>{{ t('campaignNew.wl.requestsHint') }}</InfoHint></span>
          <el-input-number v-model="w.requests" :min="0" :max="1000000" :step="100"
            size="small" controls-position="right" />
        </label>
        <label class="field">
          <span class="muted tiny">{{ t('campaignNew.wl.concurrency') }}
            <InfoHint>{{ t('campaignNew.wl.inFlight') }}</InfoHint></span>
          <el-input-number v-model="w.concurrency" :min="1" :max="4096"
            size="small" controls-position="right" />
        </label>
      </div>
      <el-collapse v-if="chosenProfile || w.schedule === 'timed'" class="wl-advanced">
        <el-collapse-item name="adv" :title="t('campaignNew.wl.advanced')">
          <div class="adv-body">
            <label v-if="w.schedule === 'timed'" class="field warmup">
              <span class="muted tiny">{{ t('campaignNew.wl.warmup') }}
                <InfoHint>{{ t('campaignNew.wl.warmupHint') }}</InfoHint></span>
              <el-input-number v-model="w.warmup_s" :min="0" :max="3600" :step="10"
                size="small" controls-position="right" />
            </label>
            <el-checkbox v-if="chosenProfile" v-model="rebuild" size="small">
              {{ t('campaignNew.wl.resample') }}
              <InfoHint :width="340">
                {{ t('campaignNew.wl.resampleHint') }}
                <template v-if="!chosenProfile.managed">
                  {{ t('campaignNew.wl.resampleOwn', { h: chosenProfile.schedule_hours }) }}
                </template>
              </InfoHint>
            </el-checkbox>
          </div>
        </el-collapse-item>
      </el-collapse>
    </template>

    <el-select v-else v-model="w.slug" size="small" filterable allow-create default-first-option
      class="mono" :placeholder="t('campaignNew.wl.slug')">
      <el-option v-for="b in benchmarks" :key="b.slug" :value="b.slug" :label="b.slug">
        <span class="mono">{{ b.slug }}</span>
        <span class="muted opt-help">
          {{ b.replays_profile ? t('campaignNew.wl.replays', { name: b.replays_profile }) : b.modules.join(' · ') }}
        </span>
      </el-option>
    </el-select>

    <div v-if="resolved" class="muted tiny">
      LLMBench benchmark <span class="mono">{{ resolved.slug }}</span>
      ({{ resolved.exists ? t('campaignNew.wl.reused') : t('campaignNew.wl.new') }})
      <InfoHint><span v-html="t('campaignNew.wl.resolvedHint')" /></InfoHint>
    </div>
  </div>
</template>

<style scoped>
.workload { display: flex; flex-direction: column; gap: 8px; }
.modes-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.existing-link { margin-left: auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px 12px; }
.field { display: flex; flex-direction: column; gap: 2px; }
.field :deep(.el-input-number) { width: 100%; }
.opt-help { margin-left: 8px; font-size: 12px; }
.row-label { font-size: 13px; color: var(--autotune-text); display: inline-flex; align-items: center; gap: 4px; }

/* The reading and what it recommends are one block: the recommendation is
   visibly the reading's conclusion, not a separate notice. */
.reading { border: 1px solid var(--autotune-border); border-radius: 6px; background: var(--autotune-bg); overflow: hidden; }
.reading :deep(.load.compact) { padding: 12px 14px 4px; }
.rec {
  display: flex; align-items: center; gap: 12px; padding: 10px 14px;
  border-top: 1px solid var(--el-color-primary-light-7);
  background: var(--el-color-primary-light-9); border-left: 3px solid var(--el-color-primary);
}
.rec.clickable { cursor: pointer; }
.rec.clickable:hover { background: var(--el-color-primary-light-8); }
.rec.applied {
  background: var(--el-color-success-light-9); border-top-color: var(--el-color-success-light-7);
  border-left-color: var(--el-color-success);
}
.rec-text { flex: 1 1 auto; min-width: 0; }
.rec-title { font-size: 13.5px; color: var(--autotune-text); }
.rec-why { font-size: 12px; color: var(--autotune-muted); margin-top: 2px; }
.rec-done { font-size: 12.5px; color: var(--el-color-success-dark-2); white-space: nowrap; }

.wl-advanced { border-top: none; border-bottom: none; }
.wl-advanced :deep(.el-collapse-item__header) { height: 30px; font-size: 12.5px; color: var(--autotune-muted); border-bottom: none; background: transparent; }
.wl-advanced :deep(.el-collapse-item__wrap) { border-bottom: none; background: transparent; }
.wl-advanced :deep(.el-collapse-item__content) { padding-bottom: 4px; }
.adv-body { display: flex; align-items: flex-end; gap: 20px; flex-wrap: wrap; }
.adv-body .warmup { width: 150px; }

/* A field the recommendation just changed pulses twice. */
.flashable { display: inline-flex; border-radius: 6px; }
.flash { animation: changed 0.8s ease-out 2; }
@keyframes changed {
  0% { box-shadow: 0 0 0 0 var(--el-color-primary-light-5); }
  35% { box-shadow: 0 0 0 5px var(--el-color-primary-light-5); }
  100% { box-shadow: 0 0 0 0 transparent; }
}
</style>
