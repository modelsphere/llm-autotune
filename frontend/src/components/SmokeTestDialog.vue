<script setup lang="ts">
/** "Can the platform reach this machine at all?" The backend walks the path a
 *  launch takes (ssh → docker → GPUs, or credential → RBAC → node → endpoint)
 *  and reports each step as it goes: the list is drawn from the plan at once,
 *  and each row turns from waiting to running to its answer. A k8s machine may
 *  also place a throwaway pod on its node — the only way a guest credential,
 *  which cannot read nodes, can confirm the node is there. */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  CircleCheckFilled, CircleCloseFilled, Loading, Remove, WarningFilled,
} from '@element-plus/icons-vue'
import InfoHint from './InfoHint.vue'
import { postLines, type Machine } from '../api/client'
import { useI18n } from '../i18n'

interface Step {
  name: string
  status: 'waiting' | 'running' | 'pass' | 'warn' | 'fail' | 'skip'
  detail: string
  ms: number | null
}

const open = defineModel<boolean>({ required: true })
const props = defineProps<{ machine: Machine | null }>()
const { t } = useI18n()

const pod = ref(false)
const steps = ref<Step[]>([])
const running = ref(false)
const ok = ref<boolean | null>(null)
const error = ref('')
const elapsed = ref(0)
let timer: ReturnType<typeof setInterval> | undefined
let abort: AbortController | undefined

function step(name: string): Step {
  let row = steps.value.find((s) => s.name === name)
  if (!row) {
    row = { name, status: 'waiting', detail: '', ms: null }
    steps.value.push(row)
  }
  return row
}

function onEvent(event: any) {
  if (event.event === 'plan') {
    steps.value = event.steps.map((name: string) => (
      { name, status: 'waiting', detail: '', ms: null }))
  } else if (event.event === 'start') {
    step(event.name).status = 'running'
  } else if (event.event === 'step') {
    Object.assign(step(event.name), { status: event.status, detail: event.detail, ms: event.ms })
  } else if (event.event === 'done') {
    ok.value = event.ok
    // The report is the record: every step, in order, with its answer.
    for (const check of event.checks ?? []) {
      Object.assign(step(check.name), {
        status: check.status, detail: check.detail, ms: check.ms ?? step(check.name).ms,
      })
    }
  }
}

async function run() {
  const machine = props.machine
  if (!machine) return
  abort?.abort()
  abort = new AbortController()
  running.value = true
  ok.value = null
  error.value = ''
  steps.value = []
  const started = performance.now()
  elapsed.value = 0
  clearInterval(timer)
  timer = setInterval(() => { elapsed.value = performance.now() - started }, 100)
  try {
    await postLines(
      `/machines/${machine.id}/smoke-test?stream=true&pod=${pod.value}`, onEvent, abort.signal)
    if (ok.value === null) error.value = t('resources.err.smoke')
  } catch (err: any) {
    if (err?.name !== 'AbortError') error.value = err?.message || t('resources.err.smoke')
  } finally {
    clearInterval(timer)
    elapsed.value = performance.now() - started
    running.value = false
  }
}

watch(open, (now) => {
  if (now) {
    pod.value = false
    void run()
  } else {
    abort?.abort()
  }
})
onBeforeUnmount(() => {
  abort?.abort()
  clearInterval(timer)
})

const seconds = (ms: number) => (ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`)
const finished = computed(() => steps.value.filter((s) => !['waiting', 'running'].includes(s.status)).length)
</script>

<template>
  <el-dialog v-model="open" :title="`${t('resources.smokeTest')} · ${machine?.name ?? ''}`" width="640px">
    <el-alert v-if="error" type="error" :closable="false" :title="error" style="margin-bottom: 12px" />
    <el-alert v-else-if="ok === true" type="success" :closable="false" show-icon
      :title="t('resources.smokeOk')" style="margin-bottom: 12px" />
    <el-alert v-else-if="ok === false" type="error" :closable="false" show-icon
      :title="t('resources.smokeBad')" style="margin-bottom: 12px" />

    <div class="progress muted tiny">
      <span v-if="running">{{ t('resources.smokeProgress', { done: finished, total: steps.length }) }}</span>
      <span v-else-if="steps.length">{{ t('resources.smokeTook') }}</span>
      <span class="mono">{{ seconds(elapsed) }}</span>
    </div>
    <el-progress v-if="running && steps.length" :percentage="Math.round(100 * finished / steps.length)"
      :show-text="false" :stroke-width="3" style="margin-bottom: 10px" />

    <div v-for="s in steps" :key="s.name" class="step" :class="s.status">
      <el-icon class="state" :size="16">
        <Loading v-if="s.status === 'running'" class="is-loading" />
        <CircleCheckFilled v-else-if="s.status === 'pass'" />
        <WarningFilled v-else-if="s.status === 'warn'" />
        <CircleCloseFilled v-else-if="s.status === 'fail'" />
        <Remove v-else />
      </el-icon>
      <span class="name">{{ s.name }}</span>
      <span class="detail tiny">
        <template v-if="s.status === 'waiting'">{{ t('resources.smokeWaiting') }}</template>
        <template v-else-if="s.status === 'running'">{{ t('resources.smokeRunning') }}</template>
        <template v-else>{{ s.detail }}</template>
      </span>
      <span class="ms mono tiny">{{ s.ms !== null && s.status !== 'skip' ? seconds(s.ms) : '' }}</span>
    </div>

    <template #footer>
      <el-checkbox v-if="machine?.driver === 'k8s'" v-model="pod"
        :disabled="running" style="margin-right: 12px">
        {{ t('resources.probePod') }}
        <InfoHint>{{ t('resources.probePodHint') }}</InfoHint>
      </el-checkbox>
      <el-button :loading="running" @click="run">{{ t('resources.runAgain') }}</el-button>
      <el-button @click="open = false">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.progress {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
}
.step {
  display: grid;
  grid-template-columns: 20px 110px 1fr auto;
  align-items: baseline;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.step:last-child {
  border-bottom: none;
}
.state {
  align-self: center;
}
.name {
  font-size: 12.5px;
}
.detail {
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}
.ms {
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.waiting .state,
.skip .state {
  color: var(--el-text-color-placeholder);
}
.waiting .name {
  color: var(--el-text-color-secondary);
}
.running .state {
  color: var(--el-color-primary);
}
.pass .state {
  color: var(--el-color-success);
}
.warn .state {
  color: var(--el-color-warning);
}
.fail .state {
  color: var(--el-color-danger);
}
.fail .detail {
  color: var(--el-color-danger);
}
@media (max-width: 560px) {
  .step {
    grid-template-columns: 20px 1fr auto;
  }
  .detail {
    grid-column: 2 / -1;
  }
}
</style>
