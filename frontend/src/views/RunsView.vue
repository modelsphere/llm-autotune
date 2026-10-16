<script setup lang="ts">
import { apiErrorText } from '../utils/apiError'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { api, type Campaign, type Run } from '../api/client'
import ListFilters from '../components/ListFilters.vue'
import { useI18n } from '../i18n'
import { useRowLink } from '../utils/nav'
import { useFirstLoad } from '../utils/load'
import { usePoll } from '../utils/poll'
import { countOptions, useQueryFilters } from '../utils/queryFilters'
import { failureLabel, isLive, runLabel, runStatus } from '../utils/status'
import { duration, exactTime, relativeTime } from '../utils/time'

const { t } = useI18n()
const runs = ref<Run[]>([])
const campaigns = ref<Campaign[]>([])
const runRows = useRowLink((row: Run) => `/campaigns/${row.campaign_id}?run=${row.id}`)

// The server filters: the list is the newest 200 runs, and a filter has to
// reach past them to the runs it asks for.
const { selected } = useQueryFilters(['status', 'campaign', 'owner'])

async function load() {
  const params = new URLSearchParams()
  for (const s of selected.value.status ?? []) params.append('status', s)
  for (const c of selected.value.campaign ?? []) params.append('campaign_id', c)
  for (const o of selected.value.owner ?? []) params.append('owner', o)
  runs.value = (await api.get('/runs', { params })).data
}

async function loadCampaigns() {
  try {
    campaigns.value = (await api.get('/campaigns')).data
  } catch {
    campaigns.value = []
  }
}

const first = useFirstLoad(load)
watch(selected, () => void load(), { deep: true })

const STATUSES = ['live', 'succeeded', 'failed', 'killed']
const filterFields = computed(() => [
  {
    key: 'status', label: t('common.status'),
    options: STATUSES.map((s) => ({
      value: s, label: s === 'live' ? t('filters.live') : runLabel(s),
    })),
  },
  {
    key: 'campaign', label: t('filters.campaign'), width: 220,
    options: campaigns.value.map((c) => ({ value: String(c.id), label: `${c.name} (#${c.id})` })),
  },
  {
    key: 'owner', label: t('filters.owner'),
    options: countOptions(campaigns.value, (c) => c.owner_name ?? '')
      .map(({ value, label }) => ({ value, label })),
  },
])

async function stopRun(run: Run) {
  try {
    await ElMessageBox.confirm(
      t('runs.stopConfirm', { id: run.id }),
      t('runs.stopTitle'),
      {
        confirmButtonText: t('runs.stopIt'),
        cancelButtonText: t('common.cancel'),
        type: 'warning',
      },
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

const poll = usePoll(load, 10000)
onMounted(() => {
  void first.run()
  void loadCampaigns()
  poll.start()
})
</script>

<template>
  <div class="page">
    <h1 class="page-title">{{ t('runs.title') }}</h1>
    <ListFilters v-model="selected" :fields="filterFields" />
    <el-table :data="runs" v-bind="runRows" style="cursor: pointer"
      v-loading="first.loading" :empty-text="first.emptyText()">
      <el-table-column prop="id" :label="t('common.id')" width="70" />
      <el-table-column :label="t('runs.campaign')" min-width="200">
        <template #default="{ row }">
          <router-link :to="`/campaigns/${row.campaign_id}`" class="nav-link">
            {{ row.campaign_name || row.campaign_id }}</router-link>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.status')" width="130">
        <template #default="{ row }">
          <el-tag :type="runStatus(row.status).type" size="small">
            {{ runLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('runs.failure')" width="130">
        <template #default="{ row }">{{ failureLabel(row.failure_class || '') }}</template>
      </el-table-column>
      <el-table-column :label="t('filters.owner')" width="110">
        <template #default="{ row }"><span class="muted">{{ row.owner_name || '—' }}</span></template>
      </el-table-column>
      <el-table-column prop="container_name" :label="t('runs.container')" min-width="160" class-name="mono" />
      <el-table-column :label="t('common.started')" min-width="120">
        <template #default="{ row }">
          <span :title="exactTime(row.started_at ?? row.created_at)">
            {{ relativeTime(row.started_at ?? row.created_at) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.took')" width="90">
        <template #default="{ row }">{{ duration(row.started_at, row.finished_at) }}</template>
      </el-table-column>
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <el-button v-if="isLive(row.status)" size="small" type="danger" plain
            @click.stop="stopRun(row)">
            {{ t('common.stop') }}
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <span class="muted">{{ first.emptyText(t('runs.noneMatch')) }}</span>
      </template>
    </el-table>
  </div>
</template>
