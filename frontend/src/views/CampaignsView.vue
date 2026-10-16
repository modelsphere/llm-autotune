<script setup lang="ts">
import { plugins } from '../plugins'
import { computed, onMounted, ref } from 'vue'
import { api, type Campaign, type Policy } from '../api/client'
import LinkButton from '../components/LinkButton.vue'
import ListFilters from '../components/ListFilters.vue'
import { useI18n } from '../i18n'
import { all, useFirstLoad } from '../utils/load'
import { useRowLink } from '../utils/nav'
import { countOptions, useQueryFilters } from '../utils/queryFilters'
import { campaignStatus } from '../utils/status'
import { exactTime, relativeTime } from '../utils/time'

const campaignRows = useRowLink((row: Campaign) => `/campaigns/${row.id}`)
const { t } = useI18n()
const campaigns = ref<Campaign[]>([])
const policies = ref<Policy[]>([])
/** Campaigns installed plugins run for something of their own (a request on
 *  a plugin's page) are left out unless asked for; each links to its page. */
const withManaged = ref(false)

async function load() {
  const [c, p] = await all(
    api.get('/campaigns', { params: { managed: withManaged.value ? 'include' : 'exclude' } }),
    api.get('/policies').catch(() => null),
  )
  campaigns.value = c.data
  policies.value = p?.data ?? []
}
const first = useFirstLoad(load)

/** The policy by name: "policy #1" meant looking it up on another page. */
function policyName(id: number): string {
  return policies.value.find((p) => p.id === id)?.name ?? `policy #${id}`
}

/** The window as an operator reads it, not as it is stored. A campaign with no
 *  clock says so plainly — that is the case where somebody has to remember to
 *  press Start, and it should not look the same as one that runs itself. */
function schedule(c: Campaign): { text: string; auto: boolean } {
  if (c.daily_start && c.daily_end) {
    return { text: `${c.daily_start} → ${c.daily_end}`, auto: true }
  }
  // Start and Run now open a window of their own, but that is not a
  // schedule: without a nightly window the campaign runs when started.
  return { text: t('campaigns.manual'), auto: false }
}

// -- filters -------------------------------------------------------------------
const STATUS_ORDER = ['active', 'scheduled', 'paused', 'draft', 'done']
const { selected, search, passes } = useQueryFilters(['status', 'owner', 'model', 'engine'])
const filterFields = computed(() => [
  {
    key: 'status', label: t('common.status'),
    // Statuses are identifiers and read the same in every language.
    options: countOptions(campaigns.value, (c) => c.status, (v) => v, STATUS_ORDER),
  },
  { key: 'owner', label: t('filters.owner'), options: countOptions(campaigns.value, (c) => c.owner_name ?? '') },
  { key: 'model', label: t('common.model'), width: 190,
    options: countOptions(campaigns.value, (c) => c.served_model_name) },
  { key: 'engine', label: t('filters.engine'), width: 130,
    options: countOptions(campaigns.value, (c) => c.engine) },
])
const shown = computed(() => {
  const text = search.value.trim().toLowerCase()
  return campaigns.value.filter((c) =>
    passes('status', c.status) && passes('owner', c.owner_name ?? '')
    && passes('model', c.served_model_name) && passes('engine', c.engine)
    && (!text || [c.name, c.served_model_name, c.image, String(c.id)]
      .some((v) => v.toLowerCase().includes(text))))
})

const running = computed(() => campaigns.value.filter((c) => c.status === 'active').length)
const waiting = computed(() => campaigns.value.filter((c) => c.status === 'scheduled').length)

onMounted(first.run)
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
      <LinkButton type="primary" to="/campaigns/new">
        {{ t('campaigns.newCampaign') }}
      </LinkButton>
    </div>

    <ListFilters v-model="selected" v-model:search="search" :fields="filterFields"
      :search-placeholder="t('filters.searchCampaigns')" :shown="shown.length"
      :total="campaigns.length" />
    <el-checkbox v-if="plugins.length" v-model="withManaged" class="managed-toggle"
      @change="load">{{ t('campaigns.includeManaged') }}</el-checkbox>

    <el-table :data="shown" v-bind="campaignRows" style="cursor: pointer"
      v-loading="first.loading" :empty-text="first.emptyText()">
      <el-table-column prop="id" :label="t('common.id')" width="70" />
      <el-table-column :label="t('common.name')" min-width="200">
        <template #default="{ row }">
          <router-link :to="`/campaigns/${row.id}`" class="nav-link">{{ row.name }}</router-link>
          <div v-if="row.managed_by" class="muted tiny">
            {{ t('campaigns.runBy', { label: row.managed_by.label || row.managed_by.plugin }) }}
          </div>
        </template>
      </el-table-column>
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
      <el-table-column :label="t('filters.owner')" width="110">
        <template #default="{ row }"><span class="muted">{{ row.owner_name || '—' }}</span></template>
      </el-table-column>
      <el-table-column :label="t('campaigns.window')" width="150">
        <template #default="{ row }">
          <span :class="schedule(row).auto ? 'mono' : 'muted'">{{ schedule(row).text }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('campaigns.search')" width="160">
        <template #default="{ row }">
          <span v-if="row.policy_id != null" class="mono">{{ policyName(row.policy_id) }}</span>
          <span v-else class="muted" :title="t('campaign.enumerates')">{{ t('campaigns.gridAll') }}</span>
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
.managed-toggle { margin: -4px 0 10px; }
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
