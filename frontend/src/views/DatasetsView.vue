<script setup lang="ts">
/** Replay datasets: samples of one model's recorded production traffic that a
 *  campaign's Replay workload sends again. LLMBench collects each sample from
 *  the gateway's request logs in VictoriaLogs; this page sets datasets up,
 *  shows what each sample holds, and takes new ones. */
import { Loading, MoreFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  api,
  type DatasetBuildState,
  type DatasetList,
  type FrozenDataset,
  type ReplayDataset,
} from '../api/client'
import DatasetDialog from '../components/DatasetDialog.vue'
import InfoHint from '../components/InfoHint.vue'
import { apiErrorText } from '../utils/apiError'
import { useFirstLoad } from '../utils/load'
import { usePoll } from '../utils/poll'
import { absoluteTime, exactTime, relativeTime } from '../utils/time'

const DOCS = 'https://github.com/modelsphere/llm-autotune/blob/main/docs/guide/datasets.md'

const route = useRoute()
const list = ref<DatasetList | null>(null)
const loadError = ref('')
const dialogOpen = ref(false)
const editing = ref<ReplayDataset | null>(null)
const builds = ref<Record<string, DatasetBuildState[] | null>>({})

const datasets = computed(() => list.value?.datasets ?? [])
const collecting = computed(() => datasets.value.some(inFlight))

/** Frozen datasets: files on LLMBench that never change, frozen there from
 *  a sample or imported from another installation. Read separately, so an
 *  LLMBench that does not share them still shows the rolling ones. */
const frozen = ref<FrozenDataset[]>([])
const frozenError = ref('')

async function load() {
  await Promise.all([loadList(), loadFrozen()])
}
async function loadList() {
  try {
    list.value = (await api.get('/datasets')).data
    loadError.value = ''
  } catch (error) {
    loadError.value = apiErrorText(error, 'Could not reach LLMBench')
  }
}
async function loadFrozen() {
  try {
    frozen.value = (await api.get('/datasets/frozen')).data.frozen
    frozenError.value = ''
  } catch (error) {
    frozen.value = []
    frozenError.value = apiErrorText(error, 'Could not read the frozen datasets')
  }
}
const first = useFirstLoad(load)

function openOnLlmbench() {
  if (list.value?.llmbench_url) {
    window.open(`${list.value.llmbench_url}/admin/replay-datasets`, '_blank', 'noopener')
  }
}

function size(bytes: number | null): string {
  if (bytes == null) return '—'
  const mb = bytes / (1 << 20)
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(mb < 10 ? 1 : 0)} MB`
}

// Every few seconds while a sample is being collected, otherwise every minute.
const fast = usePoll(() => (collecting.value ? load() : undefined), 5_000)
const slow = usePoll(load, 60_000)

onMounted(async () => {
  await first.run()
  fast.start()
  slow.start()
  if (route.query.new) openNew()
})

function inFlight(d: ReplayDataset): boolean {
  return d.last_build?.status === 'pending' || d.last_build?.status === 'running'
}

/** The last attempt failed after the sample now in use was taken. */
function lastFailed(d: ReplayDataset): boolean {
  if (d.last_build?.status !== 'failed') return false
  if (!d.current) return true
  return (d.last_build.created_at ?? '') > (d.current.built_at ?? '')
}

function nodesOf(d: ReplayDataset): string {
  return d.forwarded_to.length ? d.forwarded_to.join(', ') : 'all nodes'
}

function refreshLabel(d: ReplayDataset): string {
  if (d.external) return d.schedule_hours ? `Every ${d.schedule_hours} h, on LLMBench` : 'When asked'
  if (!d.refresh_hours) return 'When asked'
  if (d.refresh_hours === 24) return 'Every day'
  if (d.refresh_hours === 168) return 'Every week'
  return `Every ${d.refresh_hours} h`
}

function untilNext(d: ReplayDataset): string {
  if (!d.next_refresh_at) return ''
  const ms = new Date(d.next_refresh_at).getTime() - Date.now()
  if (ms <= 0) return 'due now'
  const hours = ms / 3_600_000
  if (hours < 1) return `next in ${Math.ceil(ms / 60_000)} min`
  return hours < 48 ? `next in ${Math.round(hours)} h` : `next in ${Math.round(hours / 24)} days`
}

const fmt = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString())
const pct = (x: number | null | undefined) => (x == null ? '—' : `${Math.round(x * 100)}%`)

function openNew() {
  editing.value = null
  dialogOpen.value = true
}

function openEdit(d: ReplayDataset) {
  editing.value = d
  dialogOpen.value = true
}

async function collect(d: ReplayDataset) {
  let force = false
  if (d.held_by.length) {
    const names = d.held_by.map((c) => c.name).join(', ')
    try {
      await ElMessageBox.confirm(
        `${names} is measuring on the current sample. Its later replay runs would use the new ` +
          'sample and no longer compare with the earlier ones. Collect a new sample anyway?',
        'Collect a new sample',
        { confirmButtonText: 'Collect', cancelButtonText: 'Cancel', type: 'warning' },
      )
    } catch {
      return
    }
    force = true
  }
  try {
    await api.post(`/datasets/${d.name}/collect`, null, { params: { force } })
    ElMessage.success(`Collecting a new sample of ${d.display_name || d.name}`)
    await load()
  } catch (error) {
    ElMessage.error(apiErrorText(error, 'Could not start collecting'))
  }
}

async function remove(d: ReplayDataset) {
  try {
    await ElMessageBox.confirm(
      `Delete ${d.display_name || d.name}? Finished campaigns keep their results; a campaign ` +
        'created later can no longer replay it.',
      'Delete dataset',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/datasets/${d.name}`)
    ElMessage.success('Dataset deleted')
    await load()
  } catch (error) {
    ElMessage.error(apiErrorText(error, 'Delete failed'))
  }
}

function onCommand(d: ReplayDataset, command: string) {
  if (command === 'delete') void remove(d)
  if (command === 'llmbench' && list.value?.llmbench_url) {
    window.open(`${list.value.llmbench_url}/admin/replay-datasets`, '_blank', 'noopener')
  }
}

async function onExpand(d: ReplayDataset, expanded: ReplayDataset[]) {
  if (!expanded.includes(d)) return
  try {
    builds.value[d.name] = (await api.get(`/datasets/${d.name}/builds`)).data
  } catch {
    builds.value[d.name] = []
  }
}

function bandRows(d: ReplayDataset): { band: string; count: number; share: number }[] {
  const buckets = d.current?.buckets ?? {}
  const total = Object.values(buckets).reduce((a, b) => a + b, 0) || 1
  return Object.entries(buckets).map(([band, count]) => ({ band, count, share: count / total }))
}
</script>

<template>
  <div class="page">
    <div class="header-row">
      <h1 class="page-title">Datasets
        <InfoHint :width="400">
          A dataset is a sample of one model's recorded production requests, which a campaign's
          <b>Replay</b> workload sends again. LLMBench collects it from the gateway's request
          logs in VictoriaLogs, and replaces it with a fresh sample on the interval you choose,
          but not while a campaign is measuring on it.
          <a :href="DOCS" target="_blank" rel="noopener">Datasets guide</a>
        </InfoHint>
      </h1>
      <el-button type="primary" @click="openNew">New dataset</el-button>
    </div>

    <el-alert v-if="loadError" type="error" :closable="false" show-icon class="banner"
      :title="loadError" />
    <el-alert v-else-if="list && !list.collector" type="warning" :closable="false" show-icon
      class="banner" title="LLMBench has no dataset collector, so no sample can be collected">
      An admin turns it on in LLMBench's values (<span class="mono">datasets.enabled</span> and
      <span class="mono">datasetBuilder.enabled</span>);
      <a :href="DOCS" target="_blank" rel="noopener">see the guide</a>.
    </el-alert>

    <el-table :data="datasets" row-key="name" @expand-change="onExpand"
      v-loading="first.loading" :empty-text="first.emptyText()">
      <el-table-column type="expand" width="36">
        <template #default="{ row }">
          <div class="detail">
            <div v-if="row.current" class="detail-grid">
              <div>
                <div class="muted tiny">Traffic window</div>
                <div>{{ absoluteTime(row.current.window_start) }} → {{ absoluteTime(row.current.window_end) }}</div>
              </div>
              <div v-if="row.current.summary?.prompt_tokens">
                <div class="muted tiny">Prompt tokens, median / p90 / longest</div>
                <div>{{ fmt(row.current.summary.prompt_tokens.p50) }} /
                  {{ fmt(row.current.summary.prompt_tokens.p90) }} /
                  {{ fmt(row.current.summary.prompt_tokens.max) }}</div>
              </div>
              <div v-if="row.current.summary?.completion_tokens">
                <div class="muted tiny">Output tokens, median / p90</div>
                <div>{{ fmt(row.current.summary.completion_tokens.p50) }} /
                  {{ fmt(row.current.summary.completion_tokens.p90) }}</div>
              </div>
              <div v-if="row.current.summary?.cache_hit_rate != null">
                <div class="muted tiny">Prefix-cache hits
                  <InfoHint>The share of prompt tokens the serving engine had cached when the
                    request was recorded.</InfoHint></div>
                <div>{{ pct(row.current.summary.cache_hit_rate) }}</div>
              </div>
            </div>
            <div v-if="bandRows(row).length" class="bands">
              <div class="muted tiny">Prompt lengths</div>
              <div v-for="b in bandRows(row)" :key="b.band" class="band">
                <span class="band-label mono tiny">{{ b.band }}</span>
                <span class="band-bar"><span :style="{ width: `${Math.max(b.share * 100, b.count ? 1 : 0)}%` }" /></span>
                <span class="tiny muted">{{ fmt(b.count) }}</span>
              </div>
            </div>
            <div class="muted tiny filters">
              Collected from <span class="mono">{{ row.source_url }}</span>
              · status {{ row.statuses.join(', ') || 'any' }} · {{ row.uris.join(', ') || 'any path' }}
              <template v-if="row.extra_logsql"> · <span class="mono">{{ row.extra_logsql }}</span></template>
            </div>
            <div class="muted tiny">Recent samples</div>
            <el-table :data="builds[row.name] ?? []" size="small" class="builds"
              :empty-text="builds[row.name] ? 'None yet' : 'Loading…'">
              <el-table-column label="Started" width="150">
                <template #default="{ row: b }">
                  <span :title="exactTime(b.created_at)">{{ absoluteTime(b.created_at) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="Result" width="110">
                <template #default="{ row: b }">
                  <el-tag size="small" effect="plain" :type="b.status === 'ready' ? 'success'
                    : b.status === 'failed' ? 'danger' : 'info'">{{ b.status }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="Requests" width="100">
                <template #default="{ row: b }">{{ fmt(b.records) }}</template>
              </el-table-column>
              <el-table-column label="Note" min-width="200">
                <template #default="{ row: b }">
                  <span class="tiny" :class="{ 'error-text': b.status === 'failed' }">
                    {{ b.error || b.progress || '' }}</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="Dataset" min-width="190">
        <template #default="{ row }">
          <div>{{ row.display_name || row.name }}
            <el-tag v-if="row.external" size="small" effect="plain" type="info"
              title="Set up on LLMBench; change it there">LLMBench</el-tag></div>
          <div class="muted tiny mono">{{ row.name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Traffic" min-width="190">
        <template #default="{ row }">
          <div>{{ row.model || '—' }}</div>
          <div class="muted tiny">{{ nodesOf(row) }} · last {{ row.window_hours }} h</div>
        </template>
      </el-table-column>
      <el-table-column label="Current sample" min-width="200">
        <template #default="{ row }">
          <template v-if="inFlight(row)">
            <div class="collecting"><el-icon class="is-loading"><Loading /></el-icon> Collecting…</div>
            <div class="muted tiny clip" :title="row.last_build?.progress ?? ''">
              {{ row.last_build?.progress || 'queued' }}</div>
          </template>
          <template v-else-if="row.current">
            <div>{{ fmt(row.current.records) }} requests</div>
            <div class="muted tiny" :title="exactTime(row.current.built_at)">
              taken {{ relativeTime(row.current.built_at) }}</div>
          </template>
          <div v-else-if="!lastFailed(row)" class="muted">Not collected yet</div>
          <div v-if="!inFlight(row) && lastFailed(row)" class="tiny error-text clip"
            :title="row.last_build?.error ?? ''">
            {{ row.current ? 'Last attempt failed: ' : 'Failed: ' }}{{ row.last_build?.error || 'no reason given' }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="New sample" min-width="150">
        <template #default="{ row }">
          <div>{{ refreshLabel(row) }}</div>
          <div v-if="row.held_by.length" class="muted tiny">
            waits for <router-link v-for="(c, i) in row.held_by" :key="c.id" class="campaign-link"
              :to="`/campaigns/${c.id}`">{{ i ? ', ' : '' }}{{ c.name }}</router-link>
          </div>
          <div v-else-if="!inFlight(row)" class="muted tiny">{{ untilNext(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Campaigns" min-width="130">
        <template #default="{ row }">
          <span v-if="!row.used_by.length" class="muted">—</span>
          <div v-for="c in row.used_by.slice(0, 3)" :key="c.id" class="tiny">
            <router-link :to="`/campaigns/${c.id}`" class="campaign-link">{{ c.name }}</router-link>
          </div>
          <div v-if="row.used_by.length > 3" class="muted tiny">and {{ row.used_by.length - 3 }} more</div>
        </template>
      </el-table-column>
      <el-table-column width="190" align="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="inFlight(row) || !list?.collector"
            @click="collect(row)">Collect now</el-button>
          <el-button v-if="row.can_edit" link type="primary" @click="openEdit(row)">Edit</el-button>
          <el-dropdown v-if="row.can_edit || list?.llmbench_url" trigger="click"
            @command="(c: string) => onCommand(row, c)">
            <el-button link class="more"><el-icon><MoreFilled /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="list?.llmbench_url" command="llmbench">Open on LLMBench</el-dropdown-item>
                <el-dropdown-item v-if="row.can_edit" command="delete" divided>
                  <span class="error-text">Delete</span></el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>

      <template #empty>
        <div v-if="first.loading" class="empty muted">{{ first.emptyText() }}</div>
        <div v-else class="empty muted">
          No datasets yet. A dataset samples one model's recorded requests from the gateway's
          logs, so a campaign can replay real traffic.
          <div><el-button link type="primary" @click="openNew">New dataset</el-button></div>
        </div>
      </template>
    </el-table>

    <div class="header-row frozen-head">
      <h2 class="section-title">Frozen datasets
        <InfoHint :width="400">
          Files on LLMBench that never change: a sample frozen there, or a dataset imported
          from another installation (an import is always frozen). Every campaign that replays
          one sees exactly the same requests, so their results compare across nights. Choose
          one under <b>Replay</b> in a campaign's workload. Import and download them on
          LLMBench's datasets page.
        </InfoHint>
      </h2>
      <el-button v-if="list?.llmbench_url" link type="primary"
        @click="openOnLlmbench">Import or download on LLMBench</el-button>
    </div>
    <el-alert v-if="frozenError" type="warning" :closable="false" show-icon class="banner"
      :title="frozenError" />
    <el-table v-else :data="frozen" row-key="name" v-loading="first.loading">
      <el-table-column label="Name" min-width="200">
        <template #default="{ row }">
          <span class="mono">{{ row.name }}</span>
          <el-tag v-if="row.origin === 'upload'" size="small" effect="plain" class="more">
            imported</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Requests" width="110">
        <template #default="{ row }">{{ fmt(row.records) }}</template>
      </el-table-column>
      <el-table-column label="Size" width="90">
        <template #default="{ row }">{{ size(row.bytes) }}</template>
      </el-table-column>
      <el-table-column label="From" min-width="220">
        <template #default="{ row }">
          <span v-if="row.source_profile || row.source_build_id" class="mono tiny">
            {{ row.source_profile }}<template v-if="row.source_build_id"> / {{ row.source_build_id }}</template>
          </span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="Frozen" width="130">
        <template #default="{ row }">
          <span :title="exactTime(row.frozen_at)">{{ relativeTime(row.frozen_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Campaigns" min-width="180">
        <template #default="{ row }">
          <template v-if="row.campaigns.length">
            <router-link v-for="(c, i) in row.campaigns" :key="c.id" :to="`/campaigns/${c.id}`"
              class="campaign-link">{{ c.name }}{{ i < row.campaigns.length - 1 ? ', ' : '' }}</router-link>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <template #empty>
        <div v-if="first.loading" class="empty muted">{{ first.emptyText() }}</div>
        <div v-else class="empty muted">
          No frozen datasets. Freeze a sample, or import a dataset downloaded from another
          installation, on LLMBench's datasets page.
        </div>
      </template>
    </el-table>

    <DatasetDialog v-model="dialogOpen" :dataset="editing" @saved="load" />
  </div>
</template>

<style scoped>
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 24px;
  margin-bottom: 12px;
}
.banner { margin-bottom: 12px; }
.frozen-head { margin-top: 32px; align-items: center; }
.section-title { font-size: 16px; font-weight: 600; margin: 0; }
.empty { padding: 24px; line-height: 1.6; }
.collecting { display: flex; align-items: center; gap: 6px; }
.clip { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.error-text { color: var(--el-color-danger); }
.campaign-link { color: var(--el-color-primary); text-decoration: none; }
.campaign-link:hover { text-decoration: underline; }
.more { margin-left: 8px; }
.detail { padding: 4px 16px 12px 52px; display: flex; flex-direction: column; gap: 10px; }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px 24px; }
.bands { display: flex; flex-direction: column; gap: 3px; max-width: 520px; }
.band { display: grid; grid-template-columns: 80px 1fr 60px; align-items: center; gap: 8px; }
.band-bar { height: 8px; background: var(--el-fill-color); border-radius: 4px; overflow: hidden; }
.band-bar span { display: block; height: 100%; background: var(--el-color-primary-light-3); }
.filters { line-height: 1.5; }
.builds { max-width: 760px; }
</style>
