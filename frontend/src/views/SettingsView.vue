<script setup lang="ts">
/** Settings (admins): where the external platforms are, and the LLM
 *  endpoints the jobs that call a model use. A value set here wins over the
 *  install's (the chart's values), which still apply while a field is empty.
 *  Checks run from the platform, not the browser: the API pods are what read
 *  VictoriaLogs and write to the catalog. */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import {
  api,
  type LlmEndpoint,
  type LlmTest,
  type PlatformCheck,
  type PlatformSettings,
} from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import { useI18n } from '../i18n'
import { apiErrorText } from '../utils/apiError'
import { useFirstLoad } from '../utils/load'

const { t } = useI18n()

const settings = ref<PlatformSettings | null>(null)
const endpoints = ref<LlmEndpoint[]>([])
const form = reactive({ victorialogs_url: '', catalog_url: '', catalog_token: '' })
const saving = ref(false)
const checks = reactive<Record<string, PlatformCheck | null>>({ victorialogs: null, catalog: null })
const checking = reactive<Record<string, boolean>>({ victorialogs: false, catalog: false })

async function load() {
  const [s, e] = await Promise.all([api.get('/settings'), api.get('/llm-endpoints')])
  settings.value = s.data
  endpoints.value = e.data
  form.victorialogs_url = s.data.victorialogs_url
  form.catalog_url = s.data.catalog_url
  form.catalog_token = ''
}
const first = useFirstLoad(load)
onMounted(first.run)

const dirty = computed(
  () =>
    !!settings.value &&
    (form.victorialogs_url.trim() !== settings.value.victorialogs_url ||
      form.catalog_url.trim().replace(/\/+$/, '') !== settings.value.catalog_url ||
      form.catalog_token.trim() !== ''),
)

async function save() {
  saving.value = true
  try {
    const body: Record<string, string> = {
      victorialogs_url: form.victorialogs_url.trim(),
      catalog_url: form.catalog_url.trim(),
    }
    if (form.catalog_token.trim()) body.catalog_token = form.catalog_token.trim()
    settings.value = (await api.put('/settings', body)).data
    form.catalog_token = ''
    ElMessage.success(t('settings.saved'))
  } catch (error) {
    ElMessage.error(apiErrorText(error, t('settings.saveFailed')))
  } finally {
    saving.value = false
  }
}

async function clearToken() {
  try {
    await ElMessageBox.confirm(t('settings.clearKeyConfirm'), t('settings.clearKey'), {
      confirmButtonText: t('settings.clearKey'), cancelButtonText: t('common.cancel'), type: 'warning',
    })
  } catch {
    return
  }
  settings.value = (await api.put('/settings', { catalog_token: '' })).data
}

async function check(target: 'victorialogs' | 'catalog') {
  checking[target] = true
  checks[target] = null
  try {
    const url = target === 'victorialogs' ? form.victorialogs_url : form.catalog_url
    const body: Record<string, string> = { target, url: url.trim() }
    if (target === 'catalog' && form.catalog_token.trim()) body.token = form.catalog_token.trim()
    checks[target] = (await api.post('/settings/check', body)).data
  } catch (error) {
    checks[target] = { ok: false, url: '', message: apiErrorText(error, t('settings.checkFailed')) }
  } finally {
    checking[target] = false
  }
}

// -- report writing -----------------------------------------------------------------

async function setReportsEndpoint(id: number | null) {
  try {
    const body = id == null ? { clear_reports_llm_endpoint: true } : { reports_llm_endpoint_id: id }
    settings.value = (await api.put('/settings', body)).data
    endpoints.value = (await api.get('/llm-endpoints')).data
  } catch (error) {
    ElMessage.error(apiErrorText(error, t('settings.saveFailed')))
  }
}

// -- LLM endpoints ------------------------------------------------------------------

const editing = ref<LlmEndpoint | null>(null)
const showEdit = ref(false)
const draft = reactive({ name: '', description: '', base_url: '', model: '', api_key: '' })
const editBusy = ref(false)

function openEdit(row: LlmEndpoint | null) {
  editing.value = row
  Object.assign(draft, {
    name: row?.name ?? '',
    description: row?.description ?? '',
    base_url: row?.base_url ?? '',
    model: row?.model ?? '',
    api_key: '',
  })
  showEdit.value = true
}

async function saveEndpoint() {
  editBusy.value = true
  try {
    const body = {
      name: draft.name.trim(),
      description: draft.description.trim(),
      base_url: draft.base_url.trim(),
      model: draft.model.trim(),
      // Left empty on an edit: keep the stored key.
      api_key: draft.api_key.trim() || (editing.value ? null : ''),
    }
    if (editing.value) await api.put(`/llm-endpoints/${editing.value.id}`, body)
    else await api.post('/llm-endpoints', body)
    showEdit.value = false
    endpoints.value = (await api.get('/llm-endpoints')).data
  } catch (error) {
    ElMessage.error(apiErrorText(error, t('settings.saveFailed')))
  } finally {
    editBusy.value = false
  }
}

async function clearKey(row: LlmEndpoint) {
  await api.put(`/llm-endpoints/${row.id}`, { ...row, api_key: '' })
  endpoints.value = (await api.get('/llm-endpoints')).data
}

async function remove(row: LlmEndpoint) {
  try {
    await ElMessageBox.confirm(t('settings.llm.deleteConfirm', { name: row.name }), t('settings.delete'), {
      confirmButtonText: t('settings.delete'), cancelButtonText: t('common.cancel'), type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.delete(`/llm-endpoints/${row.id}`)
    endpoints.value = (await api.get('/llm-endpoints')).data
  } catch (error) {
    ElMessage.error(apiErrorText(error, t('settings.llm.deleteFailed')))
  }
}

const tests = reactive<Record<number, LlmTest | 'running'>>({})
async function test(row: LlmEndpoint) {
  tests[row.id] = 'running'
  try {
    tests[row.id] = (await api.post(`/llm-endpoints/${row.id}/test`)).data
  } catch (error) {
    tests[row.id] = { ok: false, message: apiErrorText(error, t('settings.checkFailed')), seconds: null, reply: '' }
  }
}

function usedBy(row: LlmEndpoint): string {
  return row.used_by
    .map((u) => (u === 'reports' ? t('settings.llm.usedByReports') : u.replace(/^policy:/, '')))
    .join(', ')
}
</script>

<template>
  <div class="page">
    <h1 class="page-title">{{ t('settings.title') }}
      <InfoHint :width="380">{{ t('settings.about') }}</InfoHint>
    </h1>

    <el-skeleton v-if="!settings && first.loading" :rows="6" animated />
    <el-alert v-else-if="first.error" type="error" :closable="false" :title="first.error" />

    <template v-if="settings">
      <el-card shadow="never" class="block">
        <h3>{{ t('settings.platforms') }}</h3>
        <p class="muted small">{{ t('settings.platformsAbout') }}</p>
        <el-form label-position="top" @submit.prevent="save">
          <el-form-item :label="t('settings.victorialogs')">
            <div class="row">
              <el-input v-model="form.victorialogs_url" class="mono"
                :placeholder="settings.victorialogs_install || 'http://victorialogs.logging:9428'" clearable />
              <el-button :loading="checking.victorialogs" @click="check('victorialogs')">
                {{ t('settings.check') }}</el-button>
            </div>
            <div class="muted small">
              {{ t('settings.victorialogsHint') }}
              <template v-if="settings.victorialogs_install && !form.victorialogs_url">
                {{ t('settings.usingInstall', { value: settings.victorialogs_install }) }}
              </template>
            </div>
            <p v-if="checks.victorialogs" :class="checks.victorialogs.ok ? 'ok' : 'bad'">
              {{ checks.victorialogs.message }}</p>
          </el-form-item>

          <el-form-item :label="t('settings.catalog')">
            <div class="row">
              <el-input v-model="form.catalog_url" class="mono" placeholder="http://catalog.example:31556" clearable />
              <el-button :loading="checking.catalog" @click="check('catalog')">{{ t('settings.check') }}</el-button>
            </div>
            <div class="muted small">
              {{ t('settings.catalogHint') }}
              {{ t('settings.catalogReads', { value: settings.catalog_index_url || '—' }) }}
            </div>
            <p v-if="checks.catalog" :class="checks.catalog.ok ? 'ok' : 'bad'">{{ checks.catalog.message }}</p>
          </el-form-item>

          <el-form-item :label="t('settings.catalogKey')">
            <div class="row">
              <el-input v-model="form.catalog_token" type="password" show-password class="mono"
                autocomplete="new-password"
                :placeholder="settings.catalog_has_token ? t('settings.keySet') : t('settings.keyUnset')" />
              <el-button v-if="settings.catalog_has_token" type="danger" plain @click="clearToken">
                {{ t('settings.clearKey') }}</el-button>
            </div>
            <div class="muted small">{{ t('settings.catalogKeyHint') }}</div>
          </el-form-item>
          <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">
            {{ t('common.save') }}</el-button>
          <el-tag v-if="settings.catalog_writable" type="success" size="small" class="tag">
            {{ t('settings.publishOn') }}</el-tag>
        </el-form>
      </el-card>

      <el-card shadow="never" class="block">
        <div class="head">
          <h3>{{ t('settings.llm.title') }}</h3>
          <el-button type="primary" size="small" @click="openEdit(null)">{{ t('settings.llm.add') }}</el-button>
        </div>
        <p class="muted small">{{ t('settings.llm.about') }}</p>
        <el-table :data="endpoints" :empty-text="t('settings.llm.empty')">
          <el-table-column :label="t('settings.llm.name')" min-width="150">
            <template #default="{ row }">
              <b>{{ row.name }}</b>
              <div v-if="row.description" class="muted small">{{ row.description }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('settings.llm.address')" min-width="260">
            <template #default="{ row }">
              <div class="mono small">{{ row.base_url }}</div>
              <div class="mono small muted">{{ row.model }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('settings.llm.key')" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.has_api_key" size="small" type="success">{{ t('settings.keySetShort') }}</el-tag>
              <span v-else class="muted small">{{ t('settings.llm.noKey') }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('settings.llm.usedBy')" min-width="160">
            <template #default="{ row }">
              <span v-if="row.used_by.length" class="small">{{ usedBy(row) }}</span>
              <span v-else class="muted small">—</span>
            </template>
          </el-table-column>
          <el-table-column width="250" align="right">
            <template #default="{ row }">
              <el-button size="small" :loading="tests[row.id] === 'running'" @click="test(row)">
                {{ t('settings.llm.test') }}</el-button>
              <el-button size="small" @click="openEdit(row)">{{ t('settings.edit') }}</el-button>
              <el-button size="small" type="danger" plain :disabled="row.used_by.length > 0"
                :title="row.used_by.length ? t('settings.llm.inUse') : ''" @click="remove(row)">
                {{ t('settings.delete') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-for="row in endpoints" :key="`t${row.id}`">
          <p v-if="tests[row.id] && tests[row.id] !== 'running'"
            :class="(tests[row.id] as LlmTest).ok ? 'ok' : 'bad'" class="small">
            <b>{{ row.name }}:</b> {{ (tests[row.id] as LlmTest).message }}
            <span v-if="(tests[row.id] as LlmTest).reply" class="muted">
              — “{{ (tests[row.id] as LlmTest).reply }}”</span>
          </p>
        </div>

        <h4 class="uses">{{ t('settings.llm.uses') }}</h4>
        <el-form label-position="left" label-width="160px">
          <el-form-item :label="t('settings.llm.reports')">
            <el-select :model-value="settings.reports_llm_endpoint_id" clearable
              :placeholder="t('settings.llm.none')" style="width: 280px"
              @update:model-value="(v: number | '' | null) => setReportsEndpoint(v === '' ? null : v)">
              <el-option v-for="e in endpoints" :key="e.id" :label="e.name" :value="e.id" />
            </el-select>
            <span class="muted small hint">{{ t('settings.llm.reportsHint') }}</span>
          </el-form-item>
          <el-form-item :label="t('settings.llm.policies')">
            <span class="muted small">{{ t('settings.llm.policiesHint') }}
              <router-link to="/policies">{{ t('nav.policies') }}</router-link></span>
          </el-form-item>
        </el-form>
      </el-card>
    </template>

    <el-dialog v-model="showEdit" :title="editing ? t('settings.llm.editTitle') : t('settings.llm.addTitle')"
      width="520px">
      <el-form label-position="top" @submit.prevent="saveEndpoint">
        <el-form-item :label="t('settings.llm.name')">
          <el-input v-model="draft.name" placeholder="glm-5" />
        </el-form-item>
        <el-form-item :label="t('settings.llm.baseUrl')">
          <el-input v-model="draft.base_url" class="mono" placeholder="https://llm.example.com/v1" />
          <div class="muted small">{{ t('settings.llm.baseUrlHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('settings.llm.model')">
          <el-input v-model="draft.model" class="mono" placeholder="glm-5" />
        </el-form-item>
        <el-form-item :label="t('settings.llm.key')">
          <el-input v-model="draft.api_key" type="password" show-password class="mono" autocomplete="new-password"
            :placeholder="editing?.has_api_key ? t('settings.keySet') : t('settings.llm.keyOptional')" />
          <div v-if="editing?.has_api_key" class="muted small">
            {{ t('settings.llm.keepKey') }}
            <el-button link type="danger" size="small" @click="clearKey(editing); showEdit = false">
              {{ t('settings.clearKey') }}</el-button>
          </div>
        </el-form-item>
        <el-form-item :label="t('settings.llm.description')">
          <el-input v-model="draft.description" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showEdit = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="editBusy"
          :disabled="!draft.name.trim() || !draft.base_url.trim() || !draft.model.trim()"
          @click="saveEndpoint">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.block {
  margin-bottom: 20px;
  max-width: 980px;
}
.block h3 {
  margin: 0 0 4px;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.row {
  display: flex;
  gap: 8px;
  width: 100%;
  max-width: 640px;
}
.small {
  font-size: 12px;
  line-height: 1.5;
}
.hint {
  margin-left: 10px;
}
.tag {
  margin-left: 10px;
}
.uses {
  margin: 20px 0 8px;
}
.ok {
  color: var(--el-color-success);
  margin: 4px 0 0;
}
.bad {
  color: var(--el-color-danger);
  margin: 4px 0 0;
}
</style>
