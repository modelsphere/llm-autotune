<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { api, type MetricSpec, type Objective, type Redline } from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import { exactTime, relativeTime } from '../utils/time'
import { toYaml } from '../utils/yaml'

const objectives = ref<Objective[]>([])
const metrics = ref<MetricSpec[]>([])
const operators = ref<string[]>(['<=', '<', '>=', '>', '==', '!='])
const editorOpen = ref(false)
const editingId = ref<number | null>(null)
const busy = ref(false)

const form = ref({
  name: '',
  description: '',
  target_metric: '',
  // null = follow whatever the metric implies; set only when the user overrides.
  direction: null as 'maximize' | 'minimize' | null,
  redlines: [] as Redline[],
})

/** Grouped by the benchmark that reports them, headline metrics first inside
 *  each group. Flat, the list invited building an objective out of a sweep
 *  metric and a replay redline — a combination no single benchmark returns, so
 *  every run would score None against half of it. */
const grouped = computed(() => {
  const order: string[] = []
  const bins = new Map<string, MetricSpec[]>()
  for (const m of metrics.value) {
    const name = m.group || 'Other'
    if (!bins.has(name)) {
      bins.set(name, [])
      order.push(name)
    }
    bins.get(name)!.push(m)
  }
  return order.map((name) => ({
    name,
    metrics: [...bins.get(name)!].sort((a, b) => Number(b.headline) - Number(a.headline)),
  }))
})

function spec(key: string): MetricSpec | undefined {
  return metrics.value.find((m) => m.key === key)
}

function label(key: string): string {
  return spec(key)?.label ?? key
}

/** Which way the objective runs if the user does not override it. */
const impliedDirection = computed<'maximize' | 'minimize'>(() =>
  spec(form.value.target_metric)?.better === 'lower' ? 'minimize' : 'maximize',
)
const effectiveDirection = computed(() => form.value.direction ?? impliedDirection.value)

async function load() {
  objectives.value = (await api.get('/objectives')).data
}

async function loadMetrics() {
  const { data } = await api.get('/objectives/metrics')
  metrics.value = data.metrics
  operators.value = data.operators
  return data.default_target_metric as string
}

function openNew() {
  editingId.value = null
  form.value = {
    name: '',
    description: '',
    target_metric: metrics.value[0]?.key ?? '',
    direction: null,
    // Correctness is not optional: a fast service that answers wrongly is not
    // a faster service, so every new objective starts holding the pass rate.
    redlines: [{ metric: 'functional_acceptance.pass_rate', op: '>=', value: 0.99 }],
  }
  editorOpen.value = true
}

/** Built-ins are read-only, so "edit" on one opens a copy instead. */
function openEdit(objective: Objective, asCopy = false) {
  editingId.value = asCopy ? null : objective.id
  form.value = {
    name: asCopy ? `${objective.name} (copy)` : objective.name,
    description: objective.description,
    target_metric: objective.target_metric,
    direction: objective.direction,
    redlines: objective.redlines.map((r) => ({ ...r })),
  }
  editorOpen.value = true
}

function addRedline() {
  const metric = metrics.value.find(
    (m) => !form.value.redlines.some((r) => r.metric === m.key),
  )
  if (!metric) return
  form.value.redlines.push({
    metric: metric.key,
    // A "lower is better" metric is almost always an upper bound, and vice versa.
    op: metric.better === 'lower' ? '<=' : '>=',
    value: 0,
  })
}

async function save() {
  if (!form.value.name.trim()) {
    ElMessage.error('Give the objective a name')
    return
  }
  if (!form.value.target_metric) {
    ElMessage.error('Pick a metric to optimise')
    return
  }
  const body = {
    name: form.value.name.trim(),
    description: form.value.description,
    target_metric: form.value.target_metric,
    direction: effectiveDirection.value,
    redlines: form.value.redlines.filter((r) => r.metric),
  }
  busy.value = true
  try {
    if (editingId.value) await api.put(`/objectives/${editingId.value}`, body)
    else await api.post('/objectives', body)
    editorOpen.value = false
    await load()
    ElMessage.success('Saved')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Save failed')
  } finally {
    busy.value = false
  }
}

async function remove(objective: Objective) {
  try {
    await ElMessageBox.confirm(
      `Delete "${objective.name}"? Campaigns already created from it are unaffected — ` +
        `they copied the objective when they were created.`,
      'Delete objective',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/objectives/${objective.id}`)
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Delete failed')
  }
}

/** Exactly the YAML a campaign will carry — the same shape the ranker reads. */
const asYaml = computed(() =>
  toYaml({
    target_metric: form.value.target_metric,
    direction: effectiveDirection.value,
    redlines: form.value.redlines.filter((r) => r.metric),
  }),
)

function redlinesOf(objective: Objective): string {
  if (!objective.redlines.length) return 'none'
  return objective.redlines.map((r) => `${label(r.metric)} ${r.op} ${r.value}`).join(', ')
}

onMounted(() => {
  load()
  loadMetrics()
})
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">Objectives</h1>
        <span class="muted">
          What "better" means: one metric to optimise, plus the limits to stay inside.
          <InfoHint :width="360">
            A config that crosses a <b>redline</b> is rejected however fast it was.
            A campaign copies the objective it was created with, so editing one here never
            rewrites a night that already ran.
          </InfoHint>
        </span>
      </div>
      <el-button type="primary" @click="openNew">New objective</el-button>
    </div>

    <el-table :data="objectives">
      <el-table-column label="Name" min-width="220">
        <template #default="{ row }">
          <div>
            {{ row.name }}
            <el-tag v-if="row.is_builtin" size="small" type="info" class="tag">built-in</el-tag>
          </div>
          <div class="muted tiny">{{ row.description }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Objective" min-width="200">
        <template #default="{ row }">
          <span :class="row.direction === 'minimize' ? 'dir-min' : 'dir-max'">
            {{ row.direction }}
          </span>
          {{ label(row.target_metric) }}
          <div class="muted tiny mono">{{ row.target_metric }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Redlines" min-width="220">
        <template #default="{ row }">{{ redlinesOf(row) }}</template>
      </el-table-column>
      <el-table-column label="Updated" width="130">
        <template #default="{ row }">
          <span :title="exactTime(row.updated_at)">{{ relativeTime(row.updated_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="" width="200">
        <template #default="{ row }">
          <template v-if="row.is_builtin">
            <el-button size="small" @click="openEdit(row, true)">Duplicate</el-button>
          </template>
          <template v-else>
            <el-button size="small" @click="openEdit(row)">Edit</el-button>
            <el-button size="small" @click="openEdit(row, true)">Duplicate</el-button>
            <el-button size="small" type="danger" plain @click="remove(row)">Delete</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="editorOpen" :title="editingId ? 'Edit objective' : 'New objective'"
      width="760px" top="6vh">
      <el-form label-position="top">
        <div class="grid-2">
          <el-form-item label="Name">
            <el-input v-model="form.name" placeholder="e.g. throughput under a 3s TTFT SLO" />
          </el-form-item>
          <el-form-item label="Notes">
            <el-input v-model="form.description" placeholder="optional" />
          </el-form-item>
        </div>

        <el-form-item label="Objective — optimise this metric">
          <el-select v-model="form.target_metric" filterable style="width: 100%">
            <el-option-group v-for="g in grouped" :key="g.name" :label="g.name">
              <el-option v-for="m in g.metrics" :key="m.key" :value="m.key" :label="m.label">
                <span>{{ m.label }}</span>
                <span class="muted opt-help">{{ m.key }}</span>
              </el-option>
            </el-option-group>
          </el-select>
          <div v-if="spec(form.target_metric)" class="muted tiny help">
            {{ spec(form.target_metric)!.help }}
            <span v-if="spec(form.target_metric)!.unit"> ({{ spec(form.target_metric)!.unit }})</span>
          </div>
        </el-form-item>

        <el-form-item>
          <template #label>
            <span>Direction</span>
            <InfoHint>
              Auto follows the metric: latency metrics minimize, throughput metrics
              maximize.
            </InfoHint>
          </template>
          <el-radio-group v-model="form.direction">
            <el-radio-button :value="null">
              Auto ({{ impliedDirection }})
            </el-radio-button>
            <el-radio-button value="maximize">Maximize</el-radio-button>
            <el-radio-button value="minimize">Minimize</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item>
          <template #label>
            <span>Redlines</span>
            <InfoHint>
              Cross one and the run is rejected, however fast. A metric the benchmark did
              not report counts as crossed: we cannot certify an SLO we never measured.
            </InfoHint>
          </template>
          <el-table :data="form.redlines" size="small" style="width: 100%"
            empty-text="No redlines — every successful run qualifies">
            <el-table-column label="Metric" min-width="240">
              <template #default="{ row }">
                <el-select v-model="row.metric" filterable style="width: 100%">
                  <el-option-group v-for="g in grouped" :key="g.name" :label="g.name">
                    <el-option v-for="m in g.metrics" :key="m.key" :value="m.key"
                      :label="m.label">
                      <span>{{ m.label }}</span>
                      <span class="muted opt-help">{{ m.key }}</span>
                    </el-option>
                  </el-option-group>
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="Op" width="100">
              <template #default="{ row }">
                <el-select v-model="row.op">
                  <el-option v-for="op in operators" :key="op" :value="op" :label="op" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="Value" width="150">
              <template #default="{ row }">
                <el-input v-model.number="row.value" class="mono" />
              </template>
            </el-table-column>
            <el-table-column width="50">
              <template #default="{ $index }">
                <el-button size="small" text type="danger"
                  @click="form.redlines.splice($index, 1)">✕</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-button size="small" class="add" @click="addRedline">Add redline</el-button>
        </el-form-item>

        <el-collapse>
          <el-collapse-item title="As YAML — what the campaign carries">
            <pre class="mono block">{{ asYaml }}</pre>
          </el-collapse-item>
        </el-collapse>
      </el-form>

      <template #footer>
        <el-button @click="editorOpen = false">Cancel</el-button>
        <el-button type="primary" :loading="busy" @click="save">Save</el-button>
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
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.tiny {
  font-size: 11.5px;
}
.help {
  line-height: 1.5;
  margin-top: 4px;
}
.opt-help {
  margin-left: 10px;
  font-size: 12px;
  font-family: var(--autotune-mono, monospace);
}
.tag {
  margin-left: 6px;
}
.dir-max {
  color: var(--el-color-success);
  font-weight: 600;
}
.dir-min {
  color: var(--el-color-warning);
  font-weight: 600;
}
.add {
  margin-top: 8px;
}
.block {
  background: #f1f5f9;
  padding: 12px;
  border-radius: 6px;
  max-height: 220px;
  overflow: auto;
}
</style>
