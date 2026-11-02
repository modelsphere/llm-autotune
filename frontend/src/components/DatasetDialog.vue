<script setup lang="ts">
/** Set up or change a replay dataset: a sample of one model's recorded traffic
 *  that LLMBench collects from the gateway's logs in VictoriaLogs.
 *
 *  The form asks for what a person knows: the model and, if it matters, the
 *  nodes, both offered from what the log store has seen; how many requests;
 *  and how often to take a new sample. The rest of a collection profile has
 *  defaults that suit most traffic and waits under Advanced. While the model
 *  and filters change, the form counts what they match, so a typo shows up
 *  here as "no request matches" rather than later as an empty dataset.
 */
import { CircleCheckFilled, CircleCloseFilled, Loading, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { api, type ReplayDataset, type ReplayDatasetForm } from '../api/client'
import { useI18n } from '../i18n'
import { apiErrorText } from '../utils/apiError'
import InfoHint from './InfoHint.vue'

const props = defineProps<{
  /** The dataset to change; none = a new one. */
  dataset?: ReplayDataset | null
}>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [name: string] }>()

interface Seen { value: string; count: number }
interface Check { matched: number; hours: number; error?: string | null }

const { t } = useI18n()
const REFRESH = computed(() => [
  { value: 6, label: t('datasets.dialog.every6') },
  { value: 12, label: t('datasets.dialog.every12') },
  { value: 24, label: t('datasets.card.everyDay') },
  { value: 168, label: t('datasets.card.everyWeek') },
  { value: 0, label: t('datasets.card.onlyWhenAsked') },
])

function blank(): ReplayDatasetForm {
  return {
    model: '', forwarded_to: [], sample_size: 2000, window_hours: 24, refresh_hours: 24,
    display_name: '', name: '', source_url: '', statuses: ['200'],
    uris: ['/v1/chat/completions'], extra_logsql: '', max_model_len: 262144,
    min_records: 100, keep_builds: 7, subwindow_minutes: 60, max_carry_multiple: 4,
    window_timezone: 'UTC',
  }
}

const form = ref<ReplayDatasetForm>(blank())
const advanced = ref<string[]>([])
const busy = ref(false)
/** The install's VictoriaLogs address; empty = this form asks for one. */
const defaultStore = ref<string | null>(null)
const models = ref<Seen[]>([])
const nodes = ref<Seen[]>([])
const loadingModels = ref(false)
const loadingNodes = ref(false)
const storeError = ref('')
const check = ref<Check | null>(null)
const checking = ref(false)

const editing = computed(() => Boolean(props.dataset))
const refreshOptions = computed(() =>
  REFRESH.value.some((o) => o.value === form.value.refresh_hours)
    ? REFRESH.value
    : [...REFRESH.value, { value: form.value.refresh_hours, label: t('datasets.dialog.everyN', { n: form.value.refresh_hours }) }])
const storeUrl = computed(() => form.value.source_url.trim() || defaultStore.value || '')
const canSave = computed(() => Boolean(form.value.model.trim() && storeUrl.value))

/** The ID LLMBench will know it by, as the server derives it. */
const slug = computed(() => {
  const s = (form.value.display_name || form.value.model).toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '').replace(/-{2,}/g, '-')
    .slice(0, 64).replace(/[-_]+$/, '')
  return s || 'dataset'
})

/** Requests a full window would offer at the rate the check saw. */
const expected = computed(() => {
  const c = check.value
  if (!c || c.error || !c.matched) return null
  return Math.round(c.matched * (form.value.window_hours / c.hours))
})

const fmt = (n: number) => n.toLocaleString()

watch(open, (isOpen) => {
  if (!isOpen) return
  const d = props.dataset
  form.value = d ? pick(d) : blank()
  advanced.value = []
  check.value = null
  nodes.value = []
  void loadModels()
  if (d?.model) void loadNodes()
})

function pick(d: ReplayDataset): ReplayDatasetForm {
  const out = blank()
  for (const key of Object.keys(out) as (keyof ReplayDatasetForm)[]) {
    const value = d[key]
    ;(out as any)[key] = Array.isArray(value) ? [...value] : value
  }
  return out
}

async function values(field: 'model' | 'forwarded_to') {
  const { data } = await api.post('/datasets/values', {
    field, model: form.value.model, source_url: form.value.source_url,
  })
  defaultStore.value = data.victorialogs_url || ''
  return data as { values: Seen[]; error: string | null }
}

async function loadModels() {
  loadingModels.value = true
  try {
    const data = await values('model')
    models.value = data.values
    storeError.value = data.error && storeUrl.value ? data.error : ''
  } catch (error) {
    storeError.value = apiErrorText(error, t('datasets.dialog.storeReadFailed'))
  } finally {
    loadingModels.value = false
  }
}

async function loadNodes() {
  if (!form.value.model) {
    nodes.value = []
    return
  }
  loadingNodes.value = true
  try {
    nodes.value = (await values('forwarded_to')).values
  } catch {
    nodes.value = []
  } finally {
    loadingNodes.value = false
  }
}

function onModel() {
  form.value.forwarded_to = []
  void loadNodes()
}

// Count what the filters match, a moment after they stop changing.
let timer = 0
let asked = 0
watch(
  () => JSON.stringify([form.value.model, form.value.forwarded_to, form.value.window_hours,
    form.value.statuses, form.value.uris, form.value.extra_logsql, storeUrl.value]),
  () => {
    window.clearTimeout(timer)
    if (!open.value || !form.value.model.trim() || !storeUrl.value) {
      check.value = null
      return
    }
    timer = window.setTimeout(runCheck, 600)
  },
)

async function runCheck() {
  const mine = ++asked
  checking.value = true
  try {
    const { data } = await api.post('/datasets/check', form.value)
    if (mine === asked) check.value = data
  } catch (error) {
    if (mine === asked) check.value = { matched: 0, hours: 24, error: apiErrorText(error, t('datasets.dialog.checkFailed')) }
  } finally {
    if (mine === asked) checking.value = false
  }
}

async function save() {
  busy.value = true
  try {
    if (props.dataset) {
      await api.put(`/datasets/${props.dataset.name}`, form.value)
      ElMessage.success(t('datasets.dialog.saved'))
      emit('saved', props.dataset.name)
    } else {
      const { data } = await api.post('/datasets', form.value)
      if (data.collect_error) {
        ElMessage.warning(t('datasets.dialog.firstFailed', { name: data.display_name, error: data.collect_error }))
      } else {
        ElMessage.success(t('datasets.dialog.firstCollecting', { name: data.display_name }))
      }
      emit('saved', data.name)
    }
    open.value = false
  } catch (error) {
    ElMessage.error(apiErrorText(error, t('datasets.dialog.saveFailed')))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-dialog v-model="open" :title="dataset ? t('datasets.dialog.edit', { name: dataset.display_name || dataset.name }) : t('datasets.newDataset')"
    width="580px" append-to-body>
    <el-form label-position="top" @submit.prevent>
      <el-form-item v-if="defaultStore === ''" required>
        <template #label>
          {{ t('datasets.dialog.store') }}
          <InfoHint :width="340"><span v-html="t('datasets.dialog.storeHint')" /></InfoHint>
        </template>
        <el-input v-model="form.source_url" class="mono" placeholder="http://victorialogs:9428"
          @change="loadModels" />
      </el-form-item>

      <el-form-item required>
        <template #label>
          {{ t('datasets.dialog.model') }}
          <InfoHint :width="320">{{ t('datasets.dialog.modelHint') }}</InfoHint>
        </template>
        <el-select v-model="form.model" filterable allow-create default-first-option
          :loading="loadingModels" :placeholder="t('datasets.dialog.modelPlaceholder')" @change="onModel">
          <el-option v-for="m in models" :key="m.value" :value="m.value" :label="m.value">
            <span>{{ m.value }}</span>
            <span class="muted opt-help">{{ t('datasets.dialog.perDay', { n: fmt(m.count) }) }}</span>
          </el-option>
        </el-select>
        <div v-if="storeError" class="tiny error-text">{{ t('datasets.dialog.storeError', { error: storeError }) }}</div>
      </el-form-item>

      <el-form-item>
        <template #label>
          {{ t('datasets.dialog.nodes') }}
          <InfoHint :width="320">{{ t('datasets.dialog.nodesHint') }}</InfoHint>
        </template>
        <el-select v-model="form.forwarded_to" multiple filterable allow-create default-first-option
          :loading="loadingNodes" :disabled="!form.model" :placeholder="t('datasets.dialog.nodesPlaceholder')">
          <el-option v-for="n in nodes" :key="n.value" :value="n.value" :label="n.value">
            <span class="mono">{{ n.value }}</span>
            <span class="muted opt-help">{{ t('datasets.dialog.perDay', { n: fmt(n.count) }) }}</span>
          </el-option>
        </el-select>
      </el-form-item>

      <el-form-item :label="t('datasets.dialog.sample')">
        <div class="inline">
          <el-input-number v-model="form.sample_size" :min="1" :max="1000000" :step="500"
            controls-position="right" class="num" />
          <span>{{ t('datasets.dialog.fromTheLast') }}</span>
          <el-input-number v-model="form.window_hours" :min="1" :max="720"
            controls-position="right" class="num-sm" />
          <span>{{ t('datasets.dialog.hours') }}</span>
        </div>
      </el-form-item>

      <div class="grid-2">
        <el-form-item>
          <template #label>
            {{ t('datasets.dialog.newSample') }}
            <InfoHint :width="340">{{ t('datasets.dialog.newSampleHint') }}</InfoHint>
          </template>
          <el-select v-model="form.refresh_hours">
            <el-option v-for="o in refreshOptions" :key="o.value" :value="o.value" :label="o.label" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('datasets.dialog.name')">
          <el-input v-model="form.display_name" :placeholder="form.model || 'Qwen3-8B production'" />
        </el-form-item>
      </div>

      <div v-if="form.model && storeUrl" class="check" :class="{
        ok: check && !check.error && check.matched > 0,
        warn: check && !check.error && !check.matched,
        bad: check?.error }">
        <template v-if="checking || !check">
          <el-icon class="is-loading"><Loading /></el-icon> {{ t('datasets.dialog.checking') }}
        </template>
        <template v-else-if="check.error">
          <el-icon><CircleCloseFilled /></el-icon> {{ check.error }}
        </template>
        <template v-else-if="check.matched > 0">
          <el-icon><CircleCheckFilled /></el-icon>
          {{ t('datasets.dialog.matched', { n: fmt(check.matched), h: check.hours }) }}
          <span v-if="expected !== null && expected < form.sample_size" class="muted">
            {{ t('datasets.dialog.fewer', { w: form.window_hours, n: fmt(expected) }) }}
          </span>
        </template>
        <template v-else>
          <el-icon><WarningFilled /></el-icon>
          {{ t('datasets.dialog.noMatch', { h: check.hours }) }}
        </template>
      </div>

      <el-collapse v-model="advanced" class="advanced">
        <el-collapse-item name="advanced" :title="t('datasets.dialog.advanced')">
          <div class="grid-2">
            <el-form-item v-if="defaultStore" class="span-2">
              <template #label>
                {{ t('datasets.dialog.store') }}
                <InfoHint>{{ t('datasets.dialog.storeAdvHint') }}</InfoHint>
              </template>
              <el-input v-model="form.source_url" class="mono" :placeholder="defaultStore"
                @change="loadModels" />
            </el-form-item>
            <el-form-item>
              <template #label>
                {{ t('datasets.dialog.statuses') }}
                <InfoHint>{{ t('datasets.dialog.statusesHint') }}</InfoHint>
              </template>
              <el-select v-model="form.statuses" multiple filterable allow-create
                default-first-option :placeholder="t('datasets.dialog.any')" />
            </el-form-item>
            <el-form-item>
              <template #label>
                {{ t('datasets.dialog.paths') }}
                <InfoHint>{{ t('datasets.dialog.pathsHint') }}</InfoHint>
              </template>
              <el-select v-model="form.uris" multiple filterable allow-create
                default-first-option :placeholder="t('datasets.dialog.any')" />
            </el-form-item>
            <el-form-item class="span-2">
              <template #label>
                {{ t('datasets.dialog.logsql') }}
                <InfoHint :width="320"><span v-html="t('datasets.dialog.logsqlHint')" /></InfoHint>
              </template>
              <el-input v-model="form.extra_logsql" class="mono" :placeholder="t('datasets.dialog.none')" />
            </el-form-item>
            <el-form-item>
              <template #label>
                {{ t('datasets.dialog.maxLen') }}
                <InfoHint :width="300">{{ t('datasets.dialog.maxLenHint') }}</InfoHint>
              </template>
              <el-input-number v-model="form.max_model_len" :min="2048" :step="8192"
                controls-position="right" />
            </el-form-item>
            <el-form-item>
              <template #label>
                {{ t('datasets.dialog.minRecords') }}
                <InfoHint :width="300">{{ t('datasets.dialog.minRecordsHint') }}</InfoHint>
              </template>
              <el-input-number v-model="form.min_records" :min="1" controls-position="right" />
            </el-form-item>
            <el-form-item>
              <template #label>
                {{ t('datasets.dialog.slice') }}
                <InfoHint :width="340">{{ t('datasets.dialog.sliceHint') }}</InfoHint>
              </template>
              <el-input-number v-model="form.subwindow_minutes" :min="1" :max="1440"
                controls-position="right" />
            </el-form-item>
            <el-form-item>
              <template #label>
                {{ t('datasets.dialog.carry') }}
                <InfoHint :width="340">{{ t('datasets.dialog.carryHint') }}</InfoHint>
              </template>
              <el-input-number v-model="form.max_carry_multiple" :min="1" controls-position="right" />
            </el-form-item>
            <el-form-item>
              <template #label>
                {{ t('datasets.dialog.keep') }}
                <InfoHint>{{ t('datasets.dialog.keepHint') }}</InfoHint>
              </template>
              <el-input-number v-model="form.keep_builds" :min="1" :max="100"
                controls-position="right" />
            </el-form-item>
            <el-form-item>
              <template #label>
                {{ t('datasets.dialog.timezone') }}
                <InfoHint>{{ t('datasets.dialog.timezoneHint') }}</InfoHint>
              </template>
              <el-input v-model="form.window_timezone" />
            </el-form-item>
            <el-form-item v-if="!editing" class="span-2">
              <template #label>
                {{ t('datasets.dialog.id') }}
                <InfoHint>{{ t('datasets.dialog.idHint') }}</InfoHint>
              </template>
              <el-input v-model="form.name" class="mono" :placeholder="slug" />
            </el-form-item>
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-form>
    <template #footer>
      <el-button @click="open = false">{{ t('datasets.dialog.cancel') }}</el-button>
      <el-button type="primary" :loading="busy" :disabled="!canSave" @click="save">
        {{ editing ? t('datasets.dialog.save') : t('datasets.dialog.create') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.inline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.num { width: 130px; }
.num-sm { width: 100px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
.grid-2 :deep(.el-input-number), .grid-2 :deep(.el-select) { width: 100%; }
.span-2 { grid-column: span 2; }
.opt-help { margin-left: 8px; font-size: 12px; float: right; }
.check {
  display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap;
  font-size: 13px; padding: 8px 10px; margin-bottom: 12px; border-radius: 4px;
  background: var(--el-fill-color-light); color: var(--el-text-color-regular);
}
.check .el-icon { position: relative; top: 2px; }
.check.ok .el-icon { color: var(--el-color-success); }
.check.warn .el-icon { color: var(--el-color-warning); }
.check.bad { color: var(--el-color-danger); }
.error-text { color: var(--el-color-danger); margin-top: 4px; line-height: 1.4; }
.advanced { border-top: none; }
</style>
