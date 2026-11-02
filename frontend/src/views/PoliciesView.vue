<script setup lang="ts">
import { apiErrorText } from '../utils/apiError'
/** The search algorithms a campaign can run: container images registered by
 *  name. A campaign picks one on its Search step; the platform launches it
 *  and judges every config it proposes. */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { api, type Campaign, type LlmEndpoint, type Policy } from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import { all, useFirstLoad } from '../utils/load'
import { fromYaml, toYaml, YamlError } from '../utils/yaml'

const POLICIES_REPO = 'https://github.com/modelsphere/llm-autotune-policies'
const CONTRACT_DOC =
  'https://github.com/modelsphere/llm-autotune/blob/main/docs/api/policy-contract.md'

const policies = ref<Policy[]>([])
const campaigns = ref<Campaign[]>([])
const endpoints = ref<LlmEndpoint[]>([])
const editorOpen = ref(false)
const editingId = ref<number | null>(null)
const advanced = ref<string[]>([])
const busy = ref(false)

function empty() {
  return {
    name: '',
    image: '',
    description: '',
    version: '',
    repo_url: '',
    // Off: most policies delegate engine launches to the platform. Only a
    // policy that runs engines in its own container needs cards and weights.
    gpus_in_container: false,
    needs_model: false,
    ports: 4,
    // An agent policy's model (Settings → LLM endpoints); null for one that calls none.
    llm_endpoint_id: null as number | null,
    env_text: toYaml({}),
    // Secret variables (an LLM API key): name + value. A stored one comes back
    // with its name only; leaving its value blank keeps it.
    secrets: [] as { key: string; value: string; stored: boolean }[],
  }
}
const form = ref(empty())

/** Campaigns that run each policy — a policy in use cannot be deleted. */
const usage = computed(() => {
  const out: Record<number, number> = {}
  for (const c of campaigns.value) {
    if (c.policy_id != null) out[c.policy_id] = (out[c.policy_id] ?? 0) + 1
  }
  return out
})

async function load() {
  const [p, c, e] = await all(
    api.get('/policies'),
    api.get('/campaigns').catch(() => null),
    api.get('/llm-endpoints').catch(() => null),
  )
  policies.value = p.data
  campaigns.value = c?.data ?? []
  endpoints.value = e?.data ?? []
}
const first = useFirstLoad(load)

function openNew() {
  editingId.value = null
  form.value = empty()
  advanced.value = []
  editorOpen.value = true
}

function openEdit(p: Policy) {
  editingId.value = p.id
  form.value = {
    name: p.name,
    image: p.image,
    description: p.description,
    version: p.version,
    repo_url: p.repo_url,
    gpus_in_container: p.gpus_in_container,
    needs_model: p.needs_model ?? true,
    ports: p.ports,
    llm_endpoint_id: p.llm_endpoint_id ?? null,
    env_text: toYaml(p.env ?? {}),
    secrets: (p.secret_env_keys ?? []).map((key) => ({ key, value: '', stored: true })),
  }
  advanced.value = []
  editorOpen.value = true
}

async function save() {
  busy.value = true
  try {
    const { env_text, secrets, ...rest } = form.value
    const secret_env: Record<string, string> = {}
    for (const s of secrets) if (s.key.trim()) secret_env[s.key.trim()] = s.value
    const body = {
      ...rest,
      env: fromYaml<Record<string, string>>(env_text, 'Environment') ?? {},
      secret_env,
    }
    if (editingId.value === null) await api.post('/policies', body)
    else await api.put(`/policies/${editingId.value}`, body)
    editorOpen.value = false
    ElMessage.success(editingId.value === null ? 'Policy registered' : 'Policy saved')
    await load()
  } catch (error: any) {
    ElMessage.error(
      error instanceof YamlError ? error.message : (apiErrorText(error, 'Save failed')),
    )
  } finally {
    busy.value = false
  }
}

async function remove(p: Policy) {
  try {
    await ElMessageBox.confirm(`Remove the policy ${p.name}? The image itself is untouched.`,
      'Remove policy', { confirmButtonText: 'Remove', cancelButtonText: 'Cancel', type: 'warning' })
  } catch {
    return
  }
  try {
    await api.delete(`/policies/${p.id}`)
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, 'Remove failed'))
  }
}

onMounted(first.run)
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">Policies
          <InfoHint :width="380">
            A policy decides which config to try next; the platform launches and benchmarks
            each one and decides the winner itself. Pick one on a campaign's <b>Search</b>
            step — or none, and the campaign tries every config in its space.
            <br /><br />
            Build one from the
            <a :href="POLICIES_REPO" target="_blank" rel="noopener">policies repo</a>, or write
            your own against the
            <a :href="CONTRACT_DOC" target="_blank" rel="noopener">policy contract</a>.
          </InfoHint>
        </h1>
      </div>
      <el-button type="primary" @click="openNew">Register policy</el-button>
    </div>

    <el-table :data="policies" v-loading="first.loading">
      <el-table-column label="Name" min-width="200">
        <template #default="{ row }">
          <div>{{ row.name }}
            <span v-if="row.version" class="muted tiny">{{ row.version }}</span>
            <el-tag v-if="row.gpus_in_container" size="small" type="warning" effect="plain"
              title="Its container takes the machine's GPUs: it runs engines itself">
              takes GPUs</el-tag></div>
          <div class="muted tiny">{{ row.description }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Image" min-width="300">
        <template #default="{ row }"><span class="mono tiny">{{ row.image }}</span></template>
      </el-table-column>
      <el-table-column label="Campaigns" width="110">
        <template #default="{ row }">{{ usage[row.id] ?? 0 }}</template>
      </el-table-column>
      <el-table-column width="150" align="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">Edit</el-button>
          <el-button link type="danger" :disabled="Boolean(usage[row.id])"
            :title="usage[row.id] ? 'Used by a campaign' : ''" @click="remove(row)">
            Remove
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div v-if="first.loading || first.error" class="empty muted">{{ first.emptyText() }}</div>
        <div v-else class="empty muted">
          No policies.
          <a :href="POLICIES_REPO" target="_blank" rel="noopener">Policies repo</a>
        </div>
      </template>
    </el-table>

    <el-dialog v-model="editorOpen" :title="editingId === null ? 'Register policy' : 'Edit policy'"
      width="520px">
      <el-form label-position="top">
        <el-form-item label="Name">
          <el-input v-model="form.name" placeholder="random-search" />
        </el-form-item>
        <el-form-item>
          <template #label>
            Image
            <InfoHint>Use a version tag, not <span class="mono">latest</span>, so a campaign
              re-runs the same search.</InfoHint>
          </template>
          <el-input v-model="form.image" class="mono"
            placeholder="registry.example.com/llm-autotune-policy-random-search:0.1.0" />
        </el-form-item>
        <el-form-item label="Description">
          <el-input v-model="form.description" placeholder="optional" />
        </el-form-item>
        <el-form-item label="LLM endpoint">
          <el-select v-model="form.llm_endpoint_id" clearable placeholder="none: the policy calls no model"
            style="width: 100%">
            <el-option v-for="e in endpoints" :key="e.id" :label="`${e.name} · ${e.model}`" :value="e.id" />
          </el-select>
          <div class="muted hint-line">
            For an agent policy: at launch the endpoint sets LLM_BASE_URL, LLM_MODEL and LLM_API_KEY,
            over the same names below. Admins add endpoints in
            <router-link to="/settings">Settings</router-link>.
          </div>
        </el-form-item>
        <el-collapse v-model="advanced" class="advanced">
          <el-collapse-item name="advanced" title="Advanced">
            <div class="grid-2">
              <el-form-item label="Version"><el-input v-model="form.version" /></el-form-item>
              <el-form-item label="Source repository">
                <el-input v-model="form.repo_url" placeholder="https://…" />
              </el-form-item>
            </div>
            <el-form-item>
              <el-checkbox v-model="form.needs_model">Mount the model weights</el-checkbox>
              <InfoHint :width="320">
                Off for a policy that only asks the platform to launch engines and never
                reads the weights itself — on Kubernetes the mount would pin it to a node
                that has them.
              </InfoHint>
            </el-form-item>
            <el-form-item>
              <el-checkbox v-model="form.gpus_in_container">Give the container the GPUs</el-checkbox>
              <InfoHint :width="320">
                For a policy that starts engines inside its own container. Off when it lets
                the platform launch them.
              </InfoHint>
            </el-form-item>
            <el-form-item label="Ports it may use">
              <el-input-number v-model="form.ports" :min="1" :max="16" />
            </el-form-item>
            <el-form-item label="Environment (YAML)">
              <el-input v-model="form.env_text" type="textarea" :rows="3" class="mono" />
            </el-form-item>
            <el-form-item>
              <template #label>
                <span>Secret variables</span>
                <InfoHint :width="340">
                  For credentials, such as an agent policy's LLM API key. Stored encrypted,
                  never shown again, and hidden from the container's launch command. The
                  environment above is visible to everyone who can open this page.
                </InfoHint>
              </template>
              <div class="secrets">
                <div v-for="(s, i) in form.secrets" :key="i" class="secret-row">
                  <el-input v-model="s.key" placeholder="NAME" class="mono" :disabled="s.stored" />
                  <el-input v-model="s.value" type="password" show-password class="mono"
                    :placeholder="s.stored ? 'stored — leave blank to keep' : 'value'" />
                  <el-button link type="danger" @click="form.secrets.splice(i, 1)">Remove</el-button>
                </div>
                <el-button link type="primary"
                  @click="form.secrets.push({ key: '', value: '', stored: false })">
                  Add a secret</el-button>
              </div>
            </el-form-item>
          </el-collapse-item>
        </el-collapse>
      </el-form>
      <template #footer>
        <el-button @click="editorOpen = false">Cancel</el-button>
        <el-button type="primary" :loading="busy"
          :disabled="!form.name.trim() || !form.image.trim()" @click="save">
          {{ editingId === null ? 'Register' : 'Save' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.hint-line {
  font-size: 12px;
  line-height: 1.5;
}
.secrets {
  width: 100%;
}
.secret-row {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 3fr) auto;
  gap: 8px;
  margin-bottom: 6px;
}
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 24px;
  margin-bottom: 12px;
}
.empty {
  padding: 24px;
  line-height: 1.6;
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.advanced {
  border-top: none;
}
</style>
