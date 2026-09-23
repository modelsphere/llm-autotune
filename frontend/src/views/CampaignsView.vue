<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Campaign } from '../api/client'
import { useI18n } from '../i18n'
import { campaignStatus } from '../utils/status'
import { exactTime, relativeTime } from '../utils/time'

const router = useRouter()
const { t } = useI18n()
const campaigns = ref<Campaign[]>([])

async function load() {
  campaigns.value = (await api.get('/campaigns')).data
}

/** The window as an operator reads it, not as it is stored. A campaign with no
 *  clock says so plainly — that is the case where somebody has to remember to
 *  press Start, and it should not look the same as one that runs itself. */
function schedule(c: Campaign): { text: string; auto: boolean } {
  if (c.daily_start && c.daily_end) {
    return { text: `${c.daily_start} → ${c.daily_end}`, auto: true }
  }
  if (c.window_start || c.window_end) return { text: t('campaigns.oneOff'), auto: true }
  return { text: t('campaigns.manual'), auto: false }
}

const running = computed(() => campaigns.value.filter((c) => c.status === 'active').length)
const waiting = computed(() => campaigns.value.filter((c) => c.status === 'scheduled').length)

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">{{ t('campaigns.title') }}</h1>
        <span class="muted">
          {{ t('campaigns.running', { n: running })
          }}<template v-if="waiting">{{ t('campaigns.waiting', { n: waiting }) }}</template>
        </span>
      </div>
      <el-button type="primary" @click="router.push('/campaigns/new')">
        {{ t('campaigns.newCampaign') }}
      </el-button>
    </div>

    <el-table :data="campaigns" @row-click="(row: Campaign) => router.push(`/campaigns/${row.id}`)"
      style="cursor: pointer">
      <el-table-column prop="id" :label="t('common.id')" width="70" />
      <el-table-column prop="name" :label="t('common.name')" min-width="200" />
      <el-table-column :label="t('common.model')" min-width="150">
        <template #default="{ row }">
          {{ row.served_model_name }}
          <span class="muted tiny">{{ row.engine }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.status')" width="115">
        <template #default="{ row }">
          <el-tag :type="campaignStatus(row.status).type" :class="campaignStatus(row.status).cls"
            size="small">
            {{ row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('campaigns.window')" width="150">
        <template #default="{ row }">
          <span :class="schedule(row).auto ? 'mono' : 'muted'">{{ schedule(row).text }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('campaigns.search')" width="110">
        <template #default="{ row }">
          <span v-if="row.policy_id != null" class="mono">policy #{{ row.policy_id }}</span>
          <span v-else class="muted">{{ t('campaign.enumerates') }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.created')" min-width="130">
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
  margin-bottom: 12px;
}
.tiny {
  font-size: 11.5px;
  margin-left: 6px;
}
</style>
