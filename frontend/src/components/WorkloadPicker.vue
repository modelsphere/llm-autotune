<script setup lang="ts">
/** What each candidate is measured with, said as a workload.
 *
 *  A first-time user does not know an LLMBench benchmark slug, or what a
 *  dataset profile is; they know what load they want. So this asks for that —
 *  synthetic prompts of some size, or a replay of recorded traffic — and the
 *  platform creates the matching benchmark on LLMBench when the campaign is
 *  created. Naming an existing benchmark stays possible, as the third choice.
 */
import { computed, ref, watch } from 'vue'
import { api, type DatasetProfile } from '../api/client'
import { specOf, type BenchmarkChoice, type Workload } from '../utils/workload'
import InfoHint from './InfoHint.vue'

const props = defineProps<{
  profiles: DatasetProfile[]
  benchmarks: BenchmarkChoice[]
  /** The second stage is a replay or an existing benchmark, never a sweep. */
  noSweep?: boolean
}>()
const w = defineModel<Workload>({ required: true })
/** Take a fresh sample of a rolling dataset when the campaign starts. */
const rebuild = defineModel<boolean>('rebuild', { default: true })
const emit = defineEmits<{ module: [name: string] }>()

/** What the workload becomes on LLMBench, from the API — so the page can
 *  name it and say whether it is new or reused. */
const resolved = ref<{ slug: string; name: string; module: string; exists: boolean | null } | null>(null)
let asked = 0
watch(
  () => JSON.stringify(specOf(w.value)),
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
      const { data } = await api.post('/benchmarks/spec', spec)
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

function profileNote(p: DatasetProfile): string {
  const size = p.records != null ? `${p.records} requests` : 'empty'
  return p.managed ? size : `${size} · resampled every ${p.schedule_hours} h`
}
</script>

<template>
  <div class="workload">
    <el-radio-group v-model="w.mode" size="small" class="modes">
      <el-radio-button v-if="!noSweep" value="sweep">Synthetic sweep</el-radio-button>
      <el-radio-button value="replay">Replay</el-radio-button>
      <el-radio-button value="existing">Existing benchmark</el-radio-button>
    </el-radio-group>

    <div v-if="w.mode === 'sweep'" class="grid">
      <label class="field">
        <span class="muted tiny">Input tokens
          <InfoHint>Length of each random prompt. A sweep is fast and repeatable, but blind
            to prefix-cache reuse — only a replay shows that.</InfoHint></span>
        <el-input-number v-model="w.input_tokens" :min="1" :max="1000000" :step="512"
          size="small" controls-position="right" />
      </label>
      <label class="field">
        <span class="muted tiny">Output tokens
          <InfoHint>Tokens generated per request.</InfoHint></span>
        <el-input-number v-model="w.output_tokens" :min="1" :max="100000" :step="128"
          size="small" controls-position="right" />
      </label>
      <label class="field">
        <span class="muted tiny">Concurrency
          <InfoHint>Concurrent requests, one level after another, comma-separated.</InfoHint></span>
        <el-input v-model="w.concurrencies" size="small" class="mono" placeholder="1, 4, 16, 64" />
      </label>
      <label class="field">
        <span class="muted tiny">Requests / slot
          <InfoHint :width="320">Level c sends c × this many requests, so every level is
            measured on the same sample per concurrent slot however fast the engine is.
            More requests, tighter numbers, longer runs.</InfoHint></span>
        <el-input-number v-model="w.requests_per_concurrency" :min="1" :max="10000" :step="5"
          size="small" controls-position="right" />
      </label>
      <label class="field">
        <span class="muted tiny">Max s / level
          <InfoHint>A level that has not sent its requests by then stops anyway.</InfoHint></span>
        <el-input-number v-model="w.max_seconds_per_level" :min="10" :max="7200" :step="60"
          size="small" controls-position="right" />
      </label>
    </div>

    <template v-else-if="w.mode === 'replay'">
      <label class="field">
        <span class="muted tiny">Dataset
          <InfoHint :width="380">
            Recorded production requests, sent again: the only measurement that sees real
            prompt lengths and prefix-cache reuse. Datasets are set up on LLMBench under
            <b>Replay datasets</b>; without one, the 20-request example set is used, which
            proves the pipeline but measures nothing. A campaign keeps the sample it
            started with, so every candidate sees the same requests.
          </InfoHint></span>
        <el-select v-model="w.dataset_profile" size="small" filterable>
          <el-option value="" label="Example set (20 requests)" />
          <el-option v-for="p in profiles" :key="p.name" :value="p.name"
            :label="p.display_name || p.name">
            <span>{{ p.display_name || p.name }}</span>
            <span class="muted opt-help">{{ profileNote(p) }}</span>
          </el-option>
        </el-select>
      </label>
      <div class="grid">
        <label class="field">
          <span class="muted tiny">Requests
            <InfoHint>How many requests to replay; 0 replays the whole sample.</InfoHint></span>
          <el-input-number v-model="w.requests" :min="0" :max="1000000" :step="100"
            size="small" controls-position="right" />
        </label>
        <label class="field">
          <span class="muted tiny">Concurrency
            <InfoHint>Requests in flight at once.</InfoHint></span>
          <el-input-number v-model="w.concurrency" :min="1" :max="4096"
            size="small" controls-position="right" />
        </label>
      </div>
      <el-checkbox v-if="chosenProfile" v-model="rebuild" size="small">
        Resample at start
        <InfoHint :width="340">
          Take a fresh sample when the campaign starts.
          <template v-if="!chosenProfile.managed">
            LLMBench also resamples this dataset every {{ chosenProfile.schedule_hours }} h on
            its own, so a long campaign may see it change; runs measured on a different sample
            are marked not comparable.
          </template>
        </InfoHint>
      </el-checkbox>
    </template>

    <el-select v-else v-model="w.slug" size="small" filterable allow-create default-first-option
      class="mono" placeholder="Benchmark slug">
      <el-option v-for="b in benchmarks" :key="b.slug" :value="b.slug" :label="b.slug">
        <span class="mono">{{ b.slug }}</span>
        <span class="muted opt-help">
          {{ b.replays_profile ? `replays ${b.replays_profile}` : b.modules.join(' · ') }}
        </span>
      </el-option>
    </el-select>

    <div v-if="resolved" class="muted tiny">
      LLMBench benchmark <span class="mono">{{ resolved.slug }}</span>
      ({{ resolved.exists ? 'reused' : 'new' }})
      <InfoHint>The platform creates this benchmark on LLMBench with the campaign, grouped
        under <span class="mono">llm-autotune</span>. The same workload always maps to the
        same benchmark.</InfoHint>
    </div>
  </div>
</template>

<style scoped>
.workload { display: flex; flex-direction: column; gap: 8px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px 12px; }
.field { display: flex; flex-direction: column; gap: 2px; }
.field :deep(.el-input-number) { width: 100%; }
.opt-help { margin-left: 8px; font-size: 12px; }
</style>
