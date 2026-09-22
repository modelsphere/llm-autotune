<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Baseline, type DeployFormatsCatalog } from '../api/client'
import DeployBindingPanel from '../components/DeployBindingPanel.vue'
import InfoHint from '../components/InfoHint.vue'
import { useI18n } from '../i18n'
import { campaignToYaml } from '../utils/campaignYaml'
import { exactTime, relativeTime } from '../utils/time'
import { fromYaml, toYaml } from '../utils/yaml'

const { t } = useI18n()
const router = useRouter()

const baselines = ref<Baseline[]>([])
const catalog = ref<DeployFormatsCatalog | null>(null)
const editorOpen = ref(false)
const importOpen = ref(false)
const editingId = ref<number | null>(null)
const busy = ref(false)
const parsing = ref(false)
/** Canonical GPU-type vocabulary from the backend — the same list machines use. */
const gpuTypes = ref<string[]>([])
const expanded = ref<number[]>([])

const form = ref({
  served_model_name: '',
  engine: 'sglang',
  card_type: '',
  notes: '',
  image: '',
  model_path: '',
  service_port: 0,
  // Paste helper: a whole `docker run …` line (or a bare serve command) the
  // backend compiles into every field below.
  command: '',
  // The knobs, edited as JSON text — the source of truth on save.
  argsText: '{}',
  // env + volumes, as YAML — the same shape the campaign form uses.
  extrasText: '',
})

const importForm = ref({
  served_model_name: '', engine: 'sglang', card_type: '',
  project: '', branch: '', path: 'config/model.yaml', preset: '', notes: '',
})

async function load() {
  baselines.value = (await api.get('/baselines')).data
}

function replaceRow(updated: Baseline) {
  baselines.value = baselines.value.map((b) => (b.id === updated.id ? updated : b))
}

function argsSummary(args: Record<string, unknown>): string {
  const entries = Object.entries(args)
  if (!entries.length) return '(defaults)'
  return entries.map(([k, v]) => `${k}=${v}`).join('  ')
}

function extrasOf(b: Baseline | null): string {
  const env = b?.extra_env ?? {}
  const volumes = b?.extra_volumes ?? {}
  if (!Object.keys(env).length && !Object.keys(volumes).length) return ''
  return toYaml({ env, volumes })
}

function openNew() {
  editingId.value = null
  form.value = {
    served_model_name: '', engine: 'sglang', card_type: '', notes: '', image: '', model_path: '',
    service_port: 0, command: '', argsText: '{}', extrasText: '',
  }
  editorOpen.value = true
}

function openEdit(b: Baseline) {
  editingId.value = b.id
  form.value = {
    served_model_name: b.served_model_name,
    engine: b.engine,
    card_type: b.card_type,
    notes: b.notes,
    image: b.image,
    model_path: b.model_path,
    service_port: b.service_port,
    command: '',
    argsText: JSON.stringify(b.engine_args, null, 2),
    extrasText: extrasOf(b),
  }
  editorOpen.value = true
}

/** Compile the pasted command into every field, without saving. */
async function parseCommand() {
  if (!form.value.command.trim()) return
  parsing.value = true
  try {
    const { data } = await api.post('/baselines/parse', {
      served_model_name: form.value.served_model_name || 'x', engine: form.value.engine,
      command: form.value.command,
    })
    form.value.argsText = JSON.stringify(data.engine_args, null, 2)
    if (data.image) form.value.image = data.image
    if (data.model_path) form.value.model_path = data.model_path
    if (data.service_port) form.value.service_port = data.service_port
    if (data.engine) form.value.engine = data.engine
    if (data.served_model_name && !form.value.served_model_name) form.value.served_model_name = data.served_model_name
    const extras = { env: data.extra_env ?? {}, volumes: data.extra_volumes ?? {} }
    if (Object.keys(extras.env).length || Object.keys(extras.volumes).length) form.value.extrasText = toYaml(extras)
    ElMessage.success(`Parsed — ${data.cards} card(s)`)
    for (const w of data.warnings ?? []) ElMessage.warning(w)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Could not parse that command')
  } finally {
    parsing.value = false
  }
}

async function save() {
  if (!form.value.served_model_name.trim()) {
    ElMessage.error('Give the served model name')
    return
  }
  let engine_args: Record<string, unknown>
  try {
    engine_args = JSON.parse(form.value.argsText || '{}')
  } catch {
    ElMessage.error('Engine args must be valid JSON')
    return
  }
  let extras: { env?: Record<string, string>; volumes?: Record<string, string> } = {}
  if (form.value.extrasText.trim()) {
    try {
      extras = (fromYaml(form.value.extrasText) as typeof extras) ?? {}
    } catch {
      ElMessage.error('Env/volumes must be valid YAML (env: {...}, volumes: {...})')
      return
    }
  }
  const body = {
    served_model_name: form.value.served_model_name.trim(),
    engine: form.value.engine,
    card_type: form.value.card_type.trim(),
    engine_args,
    image: form.value.image.trim(),
    model_path: form.value.model_path.trim(),
    service_port: Number(form.value.service_port) || 0,
    extra_env: extras.env ?? {},
    extra_volumes: extras.volumes ?? {},
    notes: form.value.notes,
  }
  busy.value = true
  try {
    if (editingId.value) await api.put(`/baselines/${editingId.value}`, body)
    else await api.post('/baselines', body)
    editorOpen.value = false
    await load()
    ElMessage.success('Saved')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Save failed')
  } finally {
    busy.value = false
  }
}

function openImport() {
  importForm.value = {
    served_model_name: '', engine: 'sglang', card_type: '',
    project: catalog.value?.default_project ?? '', branch: '', path: 'config/model.yaml',
    preset: catalog.value?.default_preset ?? '', notes: '',
  }
  importOpen.value = true
}

async function runImport() {
  const f = importForm.value
  if (!f.served_model_name.trim() || !f.project.trim() || !f.branch.trim()) {
    ElMessage.error('Served model, project and branch are required')
    return
  }
  busy.value = true
  try {
    const { data } = await api.post('/baselines/import', {
      served_model_name: f.served_model_name.trim(), engine: f.engine, card_type: f.card_type,
      project: f.project.trim(), branch: f.branch.trim(), path: f.path.trim(),
      format: f.preset ? { preset: f.preset } : {}, notes: f.notes,
    })
    importOpen.value = false
    await load()
    expanded.value = [data.baseline.id]
    ElMessage.success(`Imported from ${data.commit.slice(0, 10)} — ${data.baseline.cards} card(s)`)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Import failed')
  } finally {
    busy.value = false
  }
}

async function remove(b: Baseline) {
  try {
    await ElMessageBox.confirm(
      `Delete the baseline for ${b.served_model_name} / ${b.engine} / ` +
        `${b.card_type || '(any card)'}? Campaigns comparing against it will fall ` +
        `back to raw scores until one is captured again.`,
      'Delete baseline',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/baselines/${b.id}`)
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Delete failed')
  }
}

/** Start a campaign from this config: the knobs become the search space's
 *  base, the rest the campaign's fixed fields. Hands the New campaign page
 *  the same YAML its Import button takes. */
async function tuneFromThis(b: Baseline) {
  const { data } = await api.get(`/baselines/${b.id}/as-campaign`)
  const yaml = campaignToYaml({ ...data, name: `${b.served_model_name} from baseline #${b.id}` })
  sessionStorage.setItem('autotune_campaign_prefill', yaml)
  router.push('/campaigns/new')
}

const capturedNote = computed(
  () => 'Captured automatically when a machine is handed over; a hand-set one is left alone.',
)

onMounted(async () => {
  try {
    gpuTypes.value = (await api.get('/machines/gpu-types')).data.gpu_types
  } catch {
    gpuTypes.value = []
  }
  try {
    catalog.value = (await api.get('/baselines/formats')).data
  } catch {
    catalog.value = null
  }
  load()
})
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">Baselines</h1>
        <span class="muted">
          What production runs, per model + engine + card type — the reference a campaign
          compares its candidates against, and the file a winner is proposed into.
          <InfoHint :width="400">
            A baseline is the production config for a (served model, engine, card type): the
            whole launch config — image, weights path, knobs, env, volumes. A campaign tuning
            that combination relaunches it on the same dataset and expresses every candidate
            as a multiple of it, so <b>×1.15 of production</b> keeps its meaning across nights.
            Bound to its deploy-repo file, it mirrors what the release branch deploys, and a
            campaign winner becomes a change request against that file.
            {{ capturedNote }}
          </InfoHint>
        </span>
      </div>
      <div>
        <el-button @click="openImport">{{ t('binding.import') }}</el-button>
        <el-button type="primary" @click="openNew">New baseline</el-button>
      </div>
    </div>

    <el-table :data="baselines" row-key="id" :expand-row-keys="expanded"
      empty-text="No baselines yet — one appears on the next hand-over, import one from the deploy repo, or add one by hand">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="expand">
            <div class="facts">
              <span v-if="row.image"><span class="muted">image</span> <span class="mono">{{ row.image }}</span></span>
              <span v-if="row.model_path"><span class="muted">weights</span> <span class="mono">{{ row.model_path }}</span></span>
              <span v-if="Object.keys(row.extra_env ?? {}).length"><span class="muted">env</span>
                <span class="mono">{{ Object.entries(row.extra_env).map(([k, v]) => `${k}=${v}`).join(' ') }}</span></span>
            </div>
            <DeployBindingPanel :baseline="row" :catalog="catalog" @changed="replaceRow" />
          </div>
        </template>
      </el-table-column>
      <el-table-column label="Served model" min-width="150">
        <template #default="{ row }">
          <b>{{ row.served_model_name }}</b>
          <div class="muted tiny">{{ row.notes }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Engine" width="90">
        <template #default="{ row }">{{ row.engine }}</template>
      </el-table-column>
      <el-table-column label="Card type" width="110">
        <template #default="{ row }">{{ row.card_type || '(any)' }}</template>
      </el-table-column>
      <el-table-column label="Cards" width="70">
        <template #default="{ row }">{{ row.cards }}</template>
      </el-table-column>
      <el-table-column label="Config" min-width="280">
        <template #default="{ row }">
          <span class="mono tiny">{{ argsSummary(row.engine_args) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Source" width="200">
        <template #default="{ row }">
          <el-tag size="small" :type="row.source === 'manual' ? 'warning' : 'info'"
            effect="plain">{{ row.source }}</el-tag>
          <el-tag v-if="row.binding?.unresolved" size="small" type="warning" class="badge">
            {{ t('binding.unresolved', { n: row.binding.unresolved }) }}</el-tag>
          <el-tag v-else-if="row.binding" size="small" type="success" effect="plain" class="badge"
            :title="row.binding.branch">git</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Updated" width="120">
        <template #default="{ row }">
          <span :title="exactTime(row.updated_at ?? row.created_at)">
            {{ relativeTime(row.updated_at ?? row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="" width="260">
        <template #default="{ row }">
          <el-button size="small" @click="tuneFromThis(row)">{{ t('binding.tuneFromThis') }}</el-button>
          <el-button size="small" @click="openEdit(row)">Edit</el-button>
          <el-button size="small" type="danger" plain @click="remove(row)">Delete</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="editorOpen" :title="editingId ? 'Edit baseline' : 'New baseline'"
      width="760px" top="4vh">
      <el-form label-position="top">
        <div class="grid-3">
          <el-form-item label="Served model">
            <el-input v-model="form.served_model_name" placeholder="e.g. glm-5" />
          </el-form-item>
          <el-form-item label="Engine">
            <el-select v-model="form.engine" style="width: 100%">
              <el-option value="sglang" label="sglang" />
              <el-option value="vllm" label="vllm" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <template #label>
              <span>Card type</span>
              <InfoHint>The GPU model, e.g. A100. Leave blank to match any card.
                A baseline is only compared against runs on the same card.</InfoHint>
            </template>
            <el-select v-model="form.card_type" clearable placeholder="(any card)"
              style="width: 100%">
              <el-option v-for="t in gpuTypes" :key="t" :label="t" :value="t" />
            </el-select>
          </el-form-item>
        </div>

        <el-form-item>
          <template #label>
            <span>Paste production's command</span>
            <InfoHint>
              A whole `docker run …` line or the bare serve command. It is compiled into every
              field below: image, weights path, port, env, volumes and the engine knobs.
              Placement flags (model path in the container, host, served name) are dropped.
            </InfoHint>
          </template>
          <el-input v-model="form.command" type="textarea" :rows="2" class="mono"
            placeholder='docker run … registry.example.com/sglang:v0.5.15-cu129 python -m sglang.launch_server --tp 2 …' />
          <el-button size="small" class="add" :loading="parsing" @click="parseCommand">
            Parse into fields
          </el-button>
        </el-form-item>

        <div class="grid-3">
          <el-form-item label="Image">
            <el-input v-model="form.image" class="mono" placeholder="repository:tag" />
          </el-form-item>
          <el-form-item label="Weights path (on the machine)">
            <el-input v-model="form.model_path" class="mono" />
          </el-form-item>
          <el-form-item label="Port">
            <el-input-number v-model="form.service_port" :min="0" :max="65535" controls-position="right" style="width: 100%" />
          </el-form-item>
        </div>

        <el-form-item label="Engine args (JSON)">
          <el-input v-model="form.argsText" type="textarea" :rows="8" class="mono" />
        </el-form-item>

        <el-form-item label="Env and volumes (YAML: env: {NAME: value}, volumes: {host: container})">
          <el-input v-model="form.extrasText" type="textarea" :rows="3" class="mono" />
        </el-form-item>

        <el-form-item label="Notes">
          <el-input v-model="form.notes" placeholder="optional" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="editorOpen = false">Cancel</el-button>
        <el-button type="primary" :loading="busy" @click="save">Save</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="importOpen" :title="t('binding.import')" width="680px">
      <p class="muted">{{ t('binding.importHint') }}</p>
      <p v-if="catalog && !catalog.gitlab_configured" class="muted tiny">{{ t('binding.gitlabOff') }}</p>
      <el-form label-position="top">
        <div class="grid-3">
          <el-form-item label="Served model">
            <el-input v-model="importForm.served_model_name" />
          </el-form-item>
          <el-form-item label="Engine">
            <el-select v-model="importForm.engine" style="width: 100%">
              <el-option value="sglang" label="sglang" />
              <el-option value="vllm" label="vllm" />
            </el-select>
          </el-form-item>
          <el-form-item label="Card type">
            <el-select v-model="importForm.card_type" clearable placeholder="(any card)" style="width: 100%">
              <el-option v-for="t in gpuTypes" :key="t" :label="t" :value="t" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item :label="t('binding.project')">
          <el-input v-model="importForm.project" class="mono" />
        </el-form-item>
        <el-form-item :label="t('binding.branch')">
          <el-input v-model="importForm.branch" class="mono" placeholder="release/modelforge_0.0.2-nvidia_h100-sglang" />
        </el-form-item>
        <div class="grid-2">
          <el-form-item :label="t('binding.path')">
            <el-input v-model="importForm.path" class="mono" />
          </el-form-item>
          <el-form-item :label="t('binding.preset')">
            <el-select v-model="importForm.preset" style="width: 100%">
              <el-option v-for="p in catalog?.presets ?? []" :key="p.name" :value="p.name" :label="p.label" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="Notes">
          <el-input v-model="importForm.notes" placeholder="optional" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importOpen = false">Cancel</el-button>
        <el-button type="primary" :loading="busy" :disabled="catalog ? !catalog.gitlab_configured : false" @click="runImport">
          Import
        </el-button>
      </template>
    </el-dialog>
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
.grid-3 {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 16px;
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.tiny {
  font-size: 11.5px;
}
.add {
  margin-top: 8px;
}
.badge {
  margin-left: 6px;
}
.expand {
  padding: 4px 12px 8px;
}
.facts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 20px;
  font-size: 12px;
  margin-bottom: 8px;
}
</style>
