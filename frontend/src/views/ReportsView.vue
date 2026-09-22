<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type AgentReport } from '../api/client'
import { useI18n } from '../i18n'
import { exactTime, relativeTime } from '../utils/time'

const { t } = useI18n()
const router = useRouter()
const reports = ref<AgentReport[]>([])
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    reports.value = (await api.get('/agent/v1/reports')).data
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">{{ t('reports.title') }}</h1>
        <span class="muted">{{ t('reports.subtitle') }}</span>
      </div>
    </div>

    <el-table :data="reports" v-loading="loading" :empty-text="t('reports.empty')"
      @row-click="(row: AgentReport) => router.push(`/reports/${row.id}`)" class="clickable">
      <el-table-column :label="t('reports.title')" min-width="280">
        <template #default="{ row }">
          <router-link :to="`/reports/${row.id}`">{{ row.title }}</router-link>
          <el-tag v-if="!row.comparable" size="small" type="warning" class="tag"
            :title="t('reports.notComparableHint')">
            {{ t('reports.notComparable') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('reports.campaign')" min-width="140">
        <template #default="{ row }">
          <router-link v-if="row.campaign_id" :to="`/campaigns/${row.campaign_id}`">
            #{{ row.campaign_id }}
          </router-link>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('reports.baseline')" width="130">
        <template #default="{ row }"><span class="mono">run {{ row.baseline_run_id }}</span></template>
      </el-table-column>
      <el-table-column :label="t('reports.attempts')" min-width="160">
        <template #default="{ row }">
          <span class="mono">{{ row.attempt_run_ids.map((id: number) => `run ${id}`).join(', ') }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('reports.by')" width="140">
        <template #default="{ row }">{{ row.created_by_name || '—' }}</template>
      </el-table-column>
      <el-table-column :label="t('reports.when')" width="140">
        <template #default="{ row }">
          <span :title="exactTime(row.created_at)">{{ relativeTime(row.created_at) }}</span>
        </template>
      </el-table-column>
    </el-table>
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
.clickable :deep(.el-table__row) { cursor: pointer; }
.tag { margin-left: 8px; }
</style>
