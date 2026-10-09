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
  const size = p.records != null ? `${p.records} requests` : 'not built yet'
  return p.managed ? `${size} · fixed until rebuilt` : `${size} · refreshes on a schedule`
}
</script>

<template>
  <div class="workload">
    <el-radio-group v-model="w.mode" size="small" class="modes">
      <el-radio-button v-if="!noSweep" value="sweep">Synthetic load</el-radio-button>
      <el-radio-button value="replay">Replay real traffic</el-radio-button>
      <el-radio-button value="existing">An existing benchmark</el-radio-button>
    </el-radio-group>

    <template v-if="w.mode === 'sweep'">
      <p class="muted tiny lead">
        Random prompts of a fixed size, at each concurrency level in turn. Quick and the
        same every night — but blind to prefix-cache reuse, which only a replay shows.
      </p>
      <div class="sentence">
        <span>Prompts of</span>
        <el-input-number v-model="w.input_tokens" :min="1" :max="1000000" :step="512"
          size="small" controls-position="right" class="num" />
        <span>tokens, answers of</span>
        <el-input-number v-model="w.output_tokens" :min="1" :max="100000" :step="128"
          size="small" controls-position="right" class="num" />
        <span>tokens,</span>
      </div>
      <div class="sentence">
        <span>at concurrency</span>
        <el-input v-model="w.concurrencies" size="small" class="levels mono"
          placeholder="1, 4, 16, 64" />
        <span>,</span>
        <el-input-number v-model="w.seconds_per_level" :min="10" :max="3600" :step="30"
          size="small" controls-position="right" class="num" />
        <span>seconds each.</span>
      </div>
    </template>

    <template v-else-if="w.mode === 'replay'">
      <p class="muted tiny lead">
        Requests recorded from real traffic, sent again. Slower, and the only measurement
        that sees real prompt lengths and prefix-cache reuse.
      </p>
      <div class="field">
        <label class="muted tiny">
          Dataset
          <InfoHint :width="380">
            A dataset is a sample of your production requests that LLMBench collects from
            your logs, set up on LLMBench under <b>Replay datasets</b>. A campaign holds
            one build of it for its whole life, so every candidate sees the same requests.
          </InfoHint>
        </label>
        <el-select v-model="w.dataset_profile" size="small" filterable style="width: 100%">
          <el-option value="" label="LLMBench's example set (20 requests)">
            <span>LLMBench's example set</span>
            <span class="muted opt-help">20 requests — proves the pipeline, measures nothing</span>
          </el-option>
          <el-option v-for="p in profiles" :key="p.name" :value="p.name"
            :label="p.display_name || p.name">
            <span>{{ p.display_name || p.name }}</span>
            <span class="muted opt-help">{{ profileNote(p) }}</span>
          </el-option>
        </el-select>
        <div v-if="!profiles.length" class="muted tiny">
          No datasets on LLMBench yet — until one is set up there, a replay uses the example
          set.
        </div>
      </div>
      <div class="sentence">
        <span>Replay</span>
        <el-input-number v-model="w.requests" :min="0" :max="1000000" :step="100"
          size="small" controls-position="right" class="num" />
        <span>requests ({{ w.requests ? 'a sample' : 'all of them' }}),</span>
        <el-input-number v-model="w.concurrency" :min="1" :max="4096"
          size="small" controls-position="right" class="num" />
        <span>at a time.</span>
      </div>
      <el-checkbox v-if="chosenProfile" v-model="rebuild" size="small">
        Take a fresh sample when the campaign starts
      </el-checkbox>
    </template>

    <template v-else>
      <p class="muted tiny lead">A benchmark already on LLMBench, used as it is.</p>
      <el-select v-model="w.slug" size="small" filterable allow-create default-first-option
        class="mono" style="width: 100%" placeholder="the platform default">
        <el-option v-for="b in benchmarks" :key="b.slug" :value="b.slug" :label="b.slug">
          <span class="mono">{{ b.slug }}</span>
          <span class="muted opt-help">
            {{ b.replays_profile ? `replays ${b.replays_profile}` : b.modules.join(' · ') }}
          </span>
        </el-option>
      </el-select>
    </template>

    <div v-if="resolved" class="muted tiny becomes">
      On LLMBench: <span class="mono">{{ resolved.slug }}</span>
      <template v-if="resolved.exists">— already there, reused</template>
      <template v-else>— created with the campaign, under llm-autotune</template>
    </div>
  </div>
</template>

<style scoped>
.workload { display: flex; flex-direction: column; gap: 8px; }
.modes { margin-bottom: 2px; }
.lead { margin: 0; }
.sentence { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.num { width: 110px; }
.levels { width: 140px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.opt-help { margin-left: 8px; font-size: 12px; }
.becomes { margin-top: 2px; }
</style>
