<script setup lang="ts">
/** The runs of one campaign, embeddable: the same table the campaign page
 *  shows on its Runs tab, plus a drawer per run (launch command, results,
 *  error, captured log) and a Stop button on anything still live.
 *
 *  Polls on its own while a run is live, so a page that mounts it needs no
 *  wiring beyond the campaign id. */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  api,
  downloadText,
  fetchRunLog,
  type LogSource,
  type Run,
  type RunDetail,
} from '../api/client'
import { useI18n } from '../i18n'
import { apiErrorText } from '../utils/apiError'
import { isLive, runLabel, runStatus } from '../utils/status'
import { duration, exactTime, relativeTime } from '../utils/time'
import LogDialog from './LogDialog.vue'

const props = defineProps<{
  campaignId: number | null
  /** Used in download filenames and the log dialog's title. */
  name: string
}>()
const { t } = useI18n()

const runs = ref<Run[]>([])
const loaded = ref(false)
const selectedRun = ref<RunDetail | null>(null)
const runLog = ref('')
const logBusy = ref(false)
const drawerOpen = ref(false)
const logOpen = ref(false)

async function load() {
  if (props.campaignId == null) {
    runs.value = []
    loaded.value = true
    return
  }
  try {
    runs.value = (await api.get(`/campaigns/${props.campaignId}/runs`)).data
  } catch {
    runs.value = []
  } finally {
    loaded.value = true
  }
}

const anyLive = computed(() => runs.value.some((r) => isLive(r.status)))

async function openRun(run: Run) {
  selectedRun.value = (await api.get(`/runs/${run.id}`)).data
  drawerOpen.value = true
  await reloadLog()
}

async function reloadLog() {
  const r = selectedRun.value
  if (!r) return
  logBusy.value = true
  try {
    runLog.value = await fetchRunLog(r.id)
  } catch {
    runLog.value = ''
  } finally {
    logBusy.value = false
  }
}

function downloadRunLog() {
  const r = selectedRun.value
  if (r) downloadText(`${props.name || 'campaign'}-run-${r.id}.log`, runLog.value)
}

async function stopRun(run: Run) {
  try {
    await ElMessageBox.confirm(
      t('runs.stopConfirm', { id: run.id }), t('runs.stopTitle'),
      { confirmButtonText: t('runs.stopIt'), cancelButtonText: t('common.cancel'),
        type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.post(`/runs/${run.id}/stop`)
    ElMessage.success(t('runs.stopRequested'))
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('runs.stopFailed')))
  }
}

/** Every run's engine log, newest first, for the one-dialog view. */
const logSources = computed<LogSource[]>(() =>
  [...runs.value].sort((a, b) => b.id - a.id).map((r) => ({
    key: `run-${r.id}`,
    label: `run ${r.id} · ${runLabel(r.status)}${r.container_name ? ` · ${r.container_name}` : ''}`,
    fetch: () => fetchRunLog(r.id),
    filename: `${props.name || 'campaign'}-run-${r.id}.log`,
  })),
)

function metricCount(metrics: Record<string, unknown> | undefined): number {
  return Object.keys(metrics ?? {}).length
}

let timer = 0
onMounted(async () => {
  await load()
  // Poll only while something is live; a finished campaign's runs do not move.
  timer = window.setInterval(() => {
    if (anyLive.value) void load()
  }, 10_000)
})
onUnmounted(() => window.clearInterval(timer))
watch(() => props.campaignId, load)

defineExpose({ reload: load })
</script>

<template>
  <div class="campaign-runs">
    <div class="runs-head">
      <span class="muted tiny">
        {{ runs.length ? t('runs.count', { n: runs.length }) : (loaded ? t('runs.none') : '…') }}
      </span>
      <el-button v-if="runs.length" size="small" @click="logOpen = true">
        {{ t('runs.logs') }}
      </el-button>
    </div>
    <el-table v-if="runs.length" :data="runs" size="small" @row-click="openRun"
      style="cursor: pointer">
      <el-table-column prop="id" :label="t('common.id')" width="70" />
      <el-table-column :label="t('runs.status')" width="140">
        <template #default="{ row }">
          <el-tag :type="runStatus(row.status).type" size="small">{{ runLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="failure_class" :label="t('runs.failure')" width="120" />
      <el-table-column :label="t('runs.gpus')" width="110">
        <template #default="{ row }">
          <span class="mono">{{ row.gpu_indices?.length ? row.gpu_indices.join(',') : '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="endpoint_url" :label="t('runs.endpoint')" min-width="180"
        class-name="mono" />
      <el-table-column :label="t('runs.started')" min-width="130">
        <template #default="{ row }">
          <span :title="exactTime(row.started_at)">{{ relativeTime(row.started_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('runs.took')" width="90">
        <template #default="{ row }">{{ duration(row.started_at, row.finished_at) }}</template>
      </el-table-column>
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <el-button v-if="isLive(row.status)" size="small" type="danger" plain
            @click.stop="stopRun(row)">{{ t('runs.stop') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="drawerOpen" :title="`${t('runs.run')} ${selectedRun?.id ?? ''}`"
      size="50%" :with-header="true">
      <template v-if="selectedRun">
        <div class="facts">
          <div><span class="muted">{{ t('runs.status') }}</span>
            <el-tag :type="runStatus(selectedRun.status).type" size="small">
              {{ runLabel(selectedRun.status) }}</el-tag>
            <span v-if="selectedRun.failure_class" class="mono muted"> · {{ selectedRun.failure_class }}</span>
          </div>
          <div v-if="selectedRun.endpoint_url"><span class="muted">{{ t('runs.endpoint') }}</span>
            <span class="mono">{{ selectedRun.endpoint_url }}</span></div>
          <div v-if="selectedRun.container_name"><span class="muted">{{ t('runs.container') }}</span>
            <span class="mono">{{ selectedRun.container_name }}</span></div>
          <div v-if="selectedRun.gpu_indices?.length"><span class="muted">{{ t('runs.gpus') }}</span>
            <span class="mono">{{ selectedRun.gpu_indices.join(',') }}</span></div>
          <div><span class="muted">{{ t('runs.started') }}</span>
            <span :title="exactTime(selectedRun.started_at)">
              {{ relativeTime(selectedRun.started_at) }}</span>
            <span class="muted"> · {{ t('runs.took') }} </span>
            {{ duration(selectedRun.started_at, selectedRun.finished_at) }}
          </div>
          <div v-if="selectedRun.llmbench_submission_id">
            <span class="muted">LLMBench</span>
            <span class="mono">#{{ selectedRun.llmbench_submission_id }}</span>
          </div>
        </div>

        <h3>{{ t('runs.launchCommand') }}</h3>
        <pre class="mono block">{{ selectedRun.launch_command || t('runs.notLaunched') }}</pre>
        <h3>{{ t('runs.config') }}</h3>
        <pre class="mono block">{{ JSON.stringify(selectedRun.config, null, 2) }}</pre>
        <h3>{{ t('runs.results') }}</h3>
        <el-table v-if="selectedRun.results.length" :data="selectedRun.results" size="small">
          <el-table-column prop="source" :label="t('runs.source')" width="110" />
          <el-table-column :label="t('runs.passed')" width="90">
            <template #default="{ row }">{{ row.passed ? t('runs.yes') : t('runs.no') }}</template>
          </el-table-column>
          <el-table-column :label="t('runs.metrics')" min-width="260">
            <template #default="{ row }">
              <el-collapse v-if="metricCount(row.metrics)">
                <el-collapse-item :title="t('runs.metricCount', { n: metricCount(row.metrics) })">
                  <pre class="mono block small">{{ JSON.stringify(row.metrics, null, 2) }}</pre>
                </el-collapse-item>
              </el-collapse>
              <span v-else class="muted tiny">{{ t('runs.noMetrics') }}</span>
            </template>
          </el-table-column>
        </el-table>
        <p v-else class="muted tiny">{{ t('runs.noResults') }}</p>
        <h3 v-if="selectedRun.error">{{ t('runs.error') }}</h3>
        <pre v-if="selectedRun.error" class="mono block">{{ selectedRun.error }}</pre>
        <div class="log-head">
          <h3>{{ t('runs.capturedLog') }}</h3>
          <span>
            <el-button size="small" :loading="logBusy" @click="reloadLog">
              {{ t('common.refresh') }}</el-button>
            <el-button v-if="runLog" size="small" @click="downloadRunLog">
              {{ t('common.download') }}</el-button>
          </span>
        </div>
        <p v-if="isLive(selectedRun.status)" class="muted tiny">{{ t('runs.logWhileLive') }}</p>
        <pre class="mono block log">{{ runLog }}</pre>
      </template>
    </el-drawer>

    <LogDialog v-model="logOpen" :title="`${name} — ${t('runs.logs')}`" :sources="logSources" />
  </div>
</template>

<style scoped>
.runs-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.facts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 18px;
  margin-bottom: 8px;
}
.facts .muted {
  margin-right: 6px;
}
h3 {
  font-size: 13px;
  margin: 14px 0 6px;
}
.block {
  background: var(--el-fill-color-light);
  padding: 8px 10px;
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 12px;
  max-height: 320px;
  overflow: auto;
}
.block.small {
  max-height: 240px;
}
.block.log {
  max-height: 60vh;
}
.log-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.muted {
  color: var(--el-text-color-secondary);
}
.tiny {
  font-size: 12px;
}
</style>
