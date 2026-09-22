<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Run } from '../api/client'
import { useI18n } from '../i18n'
import { isLive, runLabel, runStatus } from '../utils/status'
import { duration, exactTime, relativeTime } from '../utils/time'

const router = useRouter()
const { t } = useI18n()
const runs = ref<Run[]>([])
let timer: number | undefined

async function load() {
  runs.value = (await api.get('/runs')).data
}

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
    ElMessage.error(error.response?.data?.detail ?? t('runs.stopFailed'))
  }
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 10000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <div class="page">
    <h1 class="page-title">{{ t('runs.title') }}</h1>
    <el-table :data="runs"
      @row-click="(row: Run) => router.push(`/campaigns/${row.campaign_id}`)"
      style="cursor: pointer">
      <el-table-column prop="id" :label="t('common.id')" width="70" />
      <el-table-column prop="campaign_id" :label="t('runs.campaign')" width="100" />
      <el-table-column :label="t('common.status')" width="140">
        <template #default="{ row }">
          <el-tag :type="runStatus(row.status).type" size="small">
            {{ runLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="failure_class" :label="t('runs.failure')" width="120" />
      <el-table-column prop="container_name" :label="t('runs.container')" min-width="170" class-name="mono" />
      <el-table-column prop="llmbench_submission_id" :label="t('runs.submission')" width="130" />
      <el-table-column :label="t('common.started')" min-width="130">
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
    </el-table>
  </div>
</template>
