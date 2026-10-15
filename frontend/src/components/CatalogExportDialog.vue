<script setup lang="ts">
/** Measured runs as model-catalog files (catalog.swiss/v2).
 *
 *  Tick the runs to publish (the campaign's baseline and its winner are
 *  ticked to start) and give each a role. The server looks the model up in the
 *  published catalog: for a model it already has, each run becomes a new
 *  variant or the next version of a published one, and metadata.yaml gains
 *  this tuning result; for a new model, the form asks for what its
 *  metadata.yaml needs (the Hugging Face repo id first) and drafts the rest.
 *  Every file is checked against the catalog's schemas and rules
 *  server-side. Nothing is pushed: download the zip, unpack it at the root of
 *  a model-catalog checkout and open the pull request.
 *
 *  Plugins open it too, with their own runs (`candidates`).
 */
import { computed, ref, watch } from 'vue'
import { api, type CatalogExportPreview, type CatalogInput } from '../api/client'
import { apiErrorText } from '../utils/apiError'
import { copyText } from '../utils/clipboard'
import { useI18n } from '../i18n'

export interface CatalogCandidate {
  run_id: number
  /** What the row is called here, and in the provenance lines. */
  label: string
  /** A short note next to the label: a score, a rank. */
  detail?: string
}
interface Row extends CatalogCandidate {
  picked: boolean
  role: string
  /** Variant id override; empty = derived (<engine>-<parallelism>-<card>-<role>). */
  id: string
  /** The image tag to publish when the run's image was named by digest alone. */
  imageTag: string
}

const props = defineProps<{
  modelValue: boolean
  candidates: CatalogCandidate[]
  /** What the dialog opens with ticked: typically [baseline, optimized]. */
  initial: { run_id: number; role: string }[]
  modelName?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', open: boolean): void }>()

const { t } = useI18n()
const open = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const rows = ref<Row[]>([])
const name = ref('')
const hf = ref('')
const displayName = ref('')
const description = ref('')
const family = ref('')
const tags = ref('')
const license = ref('')
const homepage = ref('')
const defaultVariant = ref('')
const includeEnv = ref(true)
const includeVolumes = ref(false)
/** null = automatic: 90 minutes, raised to 3x the measured load when longer. */
const startupMinutes = ref<number | null>(null)
/** Empty = the node label of the card each config was measured on. */
const gpuProduct = ref('')
/** A saved report comparing exactly these runs; a tuning result needs one. */
const reportId = ref<number | null>(null)
const advanced = ref<string[]>([])
/** The collapsed sections under the result. */
const more = ref<string[]>([])
/** Env var -> recipe (included) | site (left out), for this export: a call on
 *  a name the rules got wrong or do not know. */
const envCategories = ref<Record<string, string>>({})
/** Save those calls for every later export too. */
const rememberCalls = ref(true)

const loading = ref(false)
const downloading = ref(false)
const error = ref('')
const preview = ref<CatalogExportPreview | null>(null)
const tab = ref('')

watch(
  () => props.modelValue,
  (isOpen) => {
    if (!isOpen) return
    const initial = new Map(props.initial.map((p) => [p.run_id, p.role]))
    rows.value = props.candidates.map((c) => ({
      ...c,
      picked: initial.has(c.run_id),
      role: initial.get(c.run_id) ?? '',
      id: '',
      imageTag: '',
    }))
    for (const field of [name, hf, displayName, description, family, tags, license, homepage]) {
      field.value = ''
    }
    gpuProduct.value = ''
    reportId.value = null
    startupMinutes.value = null
    defaultVariant.value = ''
    envCategories.value = {}
    advanced.value = []
    more.value = []
    preview.value = null
    error.value = ''
    tab.value = ''
    void load()
  },
)

const picked = computed(() => rows.value.filter((r) => r.picked))
const ready = computed(() => picked.value.length > 0)
/** The form's metadata fields matter only for a model new to the catalog. */
const isNew = computed(() => !preview.value?.existing)

/** A freshly ticked row gets a role: the baseline if none is picked yet,
 *  otherwise another optimized config. */
function onTick(row: Row) {
  if (row.picked && !row.role) {
    row.role = picked.value.some((r) => r !== row && r.role === 'baseline') ? 'optimized' : 'baseline'
  }
}

function body() {
  // The baseline first, then the rest in list order.
  const order = [...picked.value].sort(
    (a, b) => Number(b.role === 'baseline') - Number(a.role === 'baseline'),
  )
  const list = (text: string) => text.split(',').map((s) => s.trim()).filter(Boolean)
  return {
    variants: order.map((r) => ({
      run_id: r.run_id, role: r.role, label: r.label, id: r.id.trim(), image_tag: r.imageTag.trim(),
    })),
    name: name.value,
    hf: hf.value,
    display_name: displayName.value,
    description: description.value,
    family: family.value,
    tags: list(tags.value),
    license: license.value,
    homepage: homepage.value,
    default_variant: defaultVariant.value,
    include_env: includeEnv.value,
    include_volumes: includeVolumes.value,
    startup_minutes: startupMinutes.value || null,
    gpu_product: list(gpuProduct.value),
    report_id: reportId.value || null,
    env_categories: envCategories.value,
  }
}

async function load() {
  if (!ready.value) {
    preview.value = null
    return
  }
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.post<CatalogExportPreview>('/catalog-export/preview', body())
    preview.value = data
    // Adopt what the server derived, so the form shows what the files say and
    // every drafted field can be edited. source.hf is never adopted: it is
    // only suggested, and confirmed by the person.
    if (!name.value) name.value = data.name
    if (!displayName.value) displayName.value = data.display_name
    if (!description.value) description.value = data.description
    if (!family.value) family.value = data.family
    if (!tags.value) tags.value = (data.tags ?? []).join(', ')
    if (defaultVariant.value && !variantIds.value.includes(defaultVariant.value)) {
      defaultVariant.value = ''
    }
    if (!data.files.some((f) => f.path === tab.value)) tab.value = data.files[0]?.path ?? ''
  } catch (e) {
    error.value = apiErrorText(e, t('catalogExport.failed'))
    preview.value = null
  } finally {
    loading.value = false
  }
}

const variantIds = computed(() => (preview.value?.variants ?? []).map((v) => v.id))

/** Every env var and mount once: the variants of one export usually share them. */
const inputs = computed(() => {
  const seen = new Map<string, CatalogInput>()
  for (const v of preview.value?.variants ?? []) {
    for (const i of v.inputs) {
      if (i.category === 'platform') continue
      seen.set(`${i.kind}:${i.name}`, i)
    }
  }
  return [...seen.values()].sort((a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name))
})
/** Env vars no rule recognizes and nobody has decided: the one question asked. */
const questions = computed(() =>
  inputs.value.filter((i) => i.kind === 'env' && i.category === 'unknown' && !envCategories.value[i.name]))
const kept = (i: CatalogInput) => i.action !== 'left out'
const summary = computed(() => {
  const count = (kind: string, inc: boolean) =>
    inputs.value.filter((i) => i.kind === kind && kept(i) === inc).length
  return t('catalogExport.inputsSummary', {
    envIn: count('env', true), envOut: count('env', false),
    mountIn: count('mount', true), mountOut: count('mount', false),
  })
})

/** Include an env var in the export, or leave it out; saved for later exports
 *  too unless "remember" is off. */
async function decide(name: string, include: boolean) {
  const category = include ? 'recipe' : 'site'
  envCategories.value = { ...envCategories.value, [name]: category }
  if (rememberCalls.value) {
    try {
      await api.put(`/catalog-export/env-categories/${encodeURIComponent(name)}`, { category })
    } catch (e) {
      error.value = apiErrorText(e, t('catalogExport.failed'))
    }
  }
  void load()
}

function useSuggestion() {
  hf.value = preview.value?.hf_suggestion ?? ''
  void load()
}

function pickReport() {
  advanced.value = ['advanced']
}

function statusText(v: CatalogExportPreview['variants'][number]) {
  return t(`catalogExport.status.${v.status}`, { version: v.version })
}

const variantFiles = computed(() => (preview.value?.files ?? []).filter((f) => f.kind !== 'report'))

async function download() {
  downloading.value = true
  error.value = ''
  try {
    const { data } = await api.post('/catalog-export/archive', body(), { responseType: 'blob' })
    const url = URL.createObjectURL(data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${preview.value?.name || 'model'}-catalog.zip`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    error.value = apiErrorText(e, t('catalogExport.failed'))
  } finally {
    downloading.value = false
  }
}
</script>

<template>
  <el-dialog v-model="open" :title="t('catalogExport.title')" width="820px" top="6vh">
    <h4>{{ t('catalogExport.configs') }}</h4>
    <el-table :data="rows" size="small" max-height="240" class="configs"
      :empty-text="t('catalogExport.noRuns')">
      <el-table-column width="44">
        <template #default="{ row }">
          <el-checkbox v-model="row.picked" @change="onTick(row)" />
        </template>
      </el-table-column>
      <el-table-column :label="t('catalogExport.runCol')" min-width="220">
        <template #default="{ row }">{{ row.label }}
          <span v-if="row.detail" class="muted">· {{ row.detail }}</span></template>
      </el-table-column>
      <el-table-column :label="t('catalogExport.role')" width="140">
        <template #default="{ row }">
          <el-select v-model="row.role" size="small" clearable :disabled="!row.picked" placeholder="—">
            <el-option value="baseline" :label="t('catalogExport.roles.baseline')" />
            <el-option value="optimized" :label="t('catalogExport.roles.optimized')" />
          </el-select>
        </template>
      </el-table-column>
    </el-table>

    <h4>{{ t('catalogExport.model') }}
      <el-tag v-if="preview" size="small" class="model-tag"
        :type="preview.catalog === 'found' ? 'success' : 'info'">
        {{ t(`catalogExport.catalog.${preview.catalog}`) }}</el-tag></h4>
    <el-form label-width="110px" size="small" class="form">
      <el-form-item :label="t('catalogExport.name')">
        <el-input v-model="name" :placeholder="modelName" class="mono" :disabled="!isNew" />
      </el-form-item>
      <el-form-item v-if="isNew" :label="t('catalogExport.hf')" required>
        <el-input v-model="hf" class="mono" placeholder="org/name" />
        <el-button v-if="preview?.hf_suggestion && !hf" size="small" link class="hint"
          @click="useSuggestion">{{ t('catalogExport.useSuggestion', { hf: preview.hf_suggestion }) }}</el-button>
      </el-form-item>
    </el-form>

    <el-collapse v-model="advanced" class="advanced">
      <el-collapse-item v-if="isNew" :title="t('catalogExport.modelDetails')" name="model">
        <el-form label-width="150px" size="small">
          <el-form-item :label="t('catalogExport.displayName')">
            <el-input v-model="displayName" maxlength="128" />
          </el-form-item>
          <el-form-item :label="t('catalogExport.descriptionField')">
            <el-input v-model="description" type="textarea" :rows="3" maxlength="2048" />
          </el-form-item>
          <el-form-item :label="t('catalogExport.familyTags')">
            <el-input v-model="family" placeholder="qwen" class="short" />
            <el-input v-model="tags" placeholder="chat, moe" class="tags" />
          </el-form-item>
          <el-form-item :label="t('catalogExport.licenseHome')">
            <el-input v-model="license" placeholder="apache-2.0" class="short" />
            <el-input v-model="homepage" placeholder="https://huggingface.co/org/name" class="tags" />
          </el-form-item>
        </el-form>
      </el-collapse-item>
      <el-collapse-item :title="t('catalogExport.advanced')" name="advanced">
        <el-form label-width="150px" size="small">
          <el-form-item :label="t('catalogExport.reportId')">
            <el-input-number v-model="reportId" :min="1" size="small" :value-on-clear="null"
              :placeholder="t('catalogExport.auto')" />
            <span class="muted tiny hint">{{ t('catalogExport.reportIdHint') }}</span>
          </el-form-item>
          <el-form-item :label="t('catalogExport.variantIds')">
            <div class="ids">
              <div v-for="row in picked" :key="row.run_id" class="id-row">
                <span class="muted tiny id-label">{{ row.label }}</span>
                <el-input v-model="row.id" size="small" class="mono" :placeholder="t('catalogExport.auto')" />
                <el-input v-model="row.imageTag" size="small" class="mono tag-input"
                  :placeholder="t('catalogExport.imageTag')" />
              </div>
            </div>
          </el-form-item>
          <el-form-item :label="t('catalogExport.defaultVariant')">
            <el-select v-model="defaultVariant" clearable :placeholder="t('catalogExport.auto')">
              <el-option v-for="id in variantIds" :key="id" :value="id" :label="id" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('catalogExport.options')">
            <el-checkbox v-model="includeEnv">{{ t('catalogExport.includeEnv') }}</el-checkbox>
            <el-checkbox v-model="includeVolumes">{{ t('catalogExport.includeVolumes') }}</el-checkbox>
          </el-form-item>
          <el-form-item :label="t('catalogExport.startup')">
            <el-input-number v-model="startupMinutes" :min="1" :max="240" size="small"
              :placeholder="t('catalogExport.auto')" :value-on-clear="null" />
          </el-form-item>
          <el-form-item label="gpuProduct">
            <el-input v-model="gpuProduct" class="mono" :placeholder="t('catalogExport.auto')" />
          </el-form-item>
        </el-form>
      </el-collapse-item>
    </el-collapse>

    <el-alert v-if="error" type="error" :closable="false" :title="error" class="note" />
    <div v-if="loading && !preview" class="muted tiny">{{ t('catalogExport.checking') }}</div>
    <template v-if="preview">
      <h4>{{ t('catalogExport.result') }}</h4>
      <el-alert v-if="preview.errors.length" type="error" :closable="false" class="note"
        :title="t('catalogExport.fixFirst')">
        <ul class="list"><li v-for="(e, i) in preview.errors" :key="i">{{ e }}</li></ul>
      </el-alert>

      <ul class="variants">
        <li v-for="v in preview.variants" :key="v.id">
          <span class="mono">{{ v.id }}</span>
          <span class="muted"> · {{ statusText(v) }}</span>
          <template v-if="v.tuning === 'recorded'">
            <span class="muted"> · </span><span class="good">{{ t('catalogExport.tuning.recorded',
              { uplift: v.uplift == null ? '' : `${v.uplift > 0 ? '+' : ''}${v.uplift}%` }) }}</span>
          </template>
          <template v-else-if="v.tuning === 'no_report'">
            <span class="muted"> · {{ t('catalogExport.tuning.no_report') }} </span>
            <el-button size="small" link type="primary" @click="pickReport">
              {{ t('catalogExport.pickReport') }}</el-button>
          </template>
          <span v-else-if="v.tuning === 'no_baseline'" class="muted"> · {{ t('catalogExport.tuning.no_baseline') }}</span>
        </li>
      </ul>

      <div v-if="questions.length" class="questions">
        <div class="q-title">{{ t('catalogExport.question') }}</div>
        <div v-for="q in questions" :key="q.name" class="q-row">
          <code class="q-name" :title="q.value">{{ q.name }}={{ q.value }}</code>
          <el-button size="small" @click="decide(q.name, true)">{{ t('catalogExport.include') }}</el-button>
          <el-button size="small" @click="decide(q.name, false)">{{ t('catalogExport.leaveOut') }}</el-button>
        </div>
        <el-checkbox v-model="rememberCalls" size="small">{{ t('catalogExport.remember') }}</el-checkbox>
      </div>

      <el-alert v-if="preview.warnings.length" type="warning" :closable="false" class="note"
        :title="t('catalogExport.check')">
        <ul class="list"><li v-for="(w, i) in preview.warnings" :key="i">{{ w }}</li></ul>
      </el-alert>

      <el-collapse v-model="more" class="more">
        <el-collapse-item v-if="inputs.length" name="inputs"
          :title="`${t('catalogExport.inputs')} · ${summary}`">
          <el-table :data="inputs" size="small" max-height="280">
            <el-table-column :label="t('catalogExport.inputName')" min-width="240">
              <template #default="{ row }">
                <span class="mono">{{ row.name }}</span>
                <div class="muted tiny mono ellipsis" :title="row.value">
                  {{ row.value }}</div>
              </template>
            </el-table-column>
            <el-table-column :label="t('catalogExport.inExport')" width="150">
              <template #default="{ row }">
                <el-switch v-if="row.kind === 'env'" :model-value="kept(row)" size="small"
                  :active-text="kept(row) ? t('catalogExport.actions.kept') : t('catalogExport.actions.left_out')"
                  @change="(v: string | number | boolean) => decide(row.name, Boolean(v))" />
                <span v-else>{{ t(`catalogExport.actions.${row.action.replace(' ', '_')}`) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('catalogExport.why')" min-width="160">
              <template #default="{ row }"><span class="muted tiny">{{ row.reason }}</span></template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
        <el-collapse-item name="files" :title="t('catalogExport.files', { n: preview.files.length })">
          <el-tabs v-model="tab">
            <el-tab-pane v-for="f in variantFiles" :key="f.path" :name="f.path" :label="f.path.split('/').pop()">
              <div class="file-actions">
                <span class="muted tiny mono">{{ f.path }}</span>
                <el-button size="small" link @click="copyText(f.text, t('catalogExport.copied'))">
                  {{ t('common.copy') }}</el-button>
              </div>
              <pre class="yaml">{{ f.text }}</pre>
            </el-tab-pane>
          </el-tabs>
        </el-collapse-item>
        <el-collapse-item v-if="preview.details.length" name="details"
          :title="t('catalogExport.detailsN', { n: preview.details.length })">
          <ul class="list muted"><li v-for="(d, i) in preview.details" :key="i">{{ d }}</li></ul>
        </el-collapse-item>
        <el-collapse-item v-if="preview.provenance?.length" name="pr" :title="t('catalogExport.provenance')">
          <div class="file-actions">
            <span />
            <el-button size="small" link
              @click="copyText(preview!.provenance.map((l) => `- ${l}`).join('\n'), t('catalogExport.copied'))">
              {{ t('common.copy') }}</el-button>
          </div>
          <ul class="list"><li v-for="(l, i) in preview.provenance" :key="i" class="mono tiny">{{ l }}</li></ul>
        </el-collapse-item>
      </el-collapse>
    </template>

    <template #footer>
      <span class="muted tiny footnote">{{ t('catalogExport.next') }}</span>
      <el-button @click="open = false">{{ t('common.close') }}</el-button>
      <el-button :loading="loading" :disabled="!ready" @click="load">{{ t('catalogExport.refresh') }}</el-button>
      <el-button type="primary" :loading="downloading"
        :disabled="!preview || loading || !preview.valid" @click="download">
        {{ t('catalogExport.download') }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
h4 { margin: 14px 0 6px; font-size: 13px; display: flex; align-items: center; gap: 8px; }
.configs { margin-bottom: 4px; }
.form { margin-top: 4px; }
.short { max-width: 140px; }
.tags { max-width: 280px; margin-left: 8px; }
.hint { margin-left: 10px; }
.advanced, .more { margin: 6px 0 10px; }
.ids { display: flex; flex-direction: column; gap: 4px; width: 100%; }
.id-row { display: flex; align-items: center; gap: 8px; }
.id-label { min-width: 140px; }
.tag-input { max-width: 170px; }
.note { margin: 8px 0; }
.list { margin: 0; padding-left: 18px; font-size: 12.5px; }
.list li { margin: 2px 0; }
.variants { list-style: none; margin: 0 0 8px; padding: 0; font-size: 13px; }
.variants li { margin: 3px 0; }
.good { color: var(--el-color-success); }
.questions { border: 1px solid var(--el-border-color-light); border-radius: 6px; padding: 8px 10px; margin: 8px 0; }
.q-title { font-size: 13px; margin-bottom: 6px; }
.q-row { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
.q-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.ellipsis { max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-actions { display: flex; justify-content: space-between; align-items: center; }
.yaml {
  max-height: 360px; overflow: auto; font-size: 12px; line-height: 1.45;
  background: var(--el-fill-color-light); padding: 10px; border-radius: 6px; margin: 0;
  white-space: pre;
}
.footnote { margin-right: auto; float: left; max-width: 340px; text-align: left; line-height: 1.4; }
.tiny { font-size: 11.5px; }
</style>
