<script setup lang="ts">
/** What a cluster still runs: shown when an edit is refused because of it, so
 *  a person can tell work in progress from work stuck tearing down. */
import { Loading, WarningFilled } from '@element-plus/icons-vue'
import LinkButton from './LinkButton.vue'
import { useI18n } from '../i18n'
import { relativeTime } from '../utils/time'

export interface LiveRun {
  id: number
  campaign_id: number
  campaign: string
  machine: string
  status: string
  teardown_pending?: boolean
  started_at: string | null
}

const open = defineModel<boolean>({ required: true })
defineProps<{ message: string; runs: LiveRun[]; sessions: LiveRun[] }>()
const { t } = useI18n()
</script>

<template>
  <el-dialog v-model="open" :title="t('resources.live.title')" width="620px">
    <p class="message">{{ message }}</p>
    <div v-for="group in [
      { key: 'runs', rows: runs, label: t('resources.live.run') },
      { key: 'sessions', rows: sessions, label: t('resources.live.session') },
    ].filter((g) => g.rows.length)" :key="group.key" class="group">
      <div v-for="row in group.rows" :key="`${group.key}-${row.id}`" class="row"
        :class="{ stuck: row.teardown_pending }">
        <el-icon v-if="row.teardown_pending" class="state warn" :size="16"><WarningFilled /></el-icon>
        <el-icon v-else class="state live is-loading" :size="16"><Loading /></el-icon>
        <span class="what">{{ group.label }} {{ row.id }}</span>
        <LinkButton link type="primary" size="small" :to="`/campaigns/${row.campaign_id}`">
          {{ row.campaign }}
        </LinkButton>
        <span class="spacer" />
        <span class="muted mono tiny">{{ row.machine }}</span>
        <el-tag size="small" :type="row.teardown_pending ? 'warning' : 'info'" effect="plain">
          {{ row.teardown_pending ? t('resources.live.tearingDown') : row.status }}
        </el-tag>
        <span class="muted tiny age" :title="row.started_at ?? ''">
          {{ row.started_at ? relativeTime(row.started_at) : '—' }}
        </span>
      </div>
    </div>
    <template #footer>
      <el-button @click="open = false">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.message {
  margin: 0 0 12px;
}
.group + .group {
  margin-top: 10px;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
}
.row:nth-child(odd) {
  background: var(--el-fill-color-lighter);
}
.row.stuck {
  background: var(--el-color-warning-light-9);
}
.state.live { color: var(--el-color-primary); }
.state.warn { color: var(--el-color-warning); }
.what {
  font-weight: 500;
}
.spacer {
  flex: 1;
}
.age {
  min-width: 72px;
  text-align: right;
}
.tiny {
  font-size: 11.5px;
}
</style>
