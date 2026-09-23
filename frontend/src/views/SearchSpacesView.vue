<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { api, type CatalogParam, type SearchSpace, type SearchSpacePreview } from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import SpaceMap from '../components/SpaceMap.vue'
import { copyText } from '../utils/clipboard'
import { exactTime, relativeTime } from '../utils/time'
import { fromYaml, toYaml, YamlError } from '../utils/yaml'

const spaces = ref<SearchSpace[]>([])
const catalog = ref<CatalogParam[]>([])
const editorOpen = ref(false)
const editingId = ref<number | null>(null)
const busy = ref(false)
const preview = ref<SearchSpacePreview | null>(null)

/** One editable row. `mode` decides whether the value is fixed for every run
 *  (base), swept independently (grid), swept in step with the other rows in
 *  its group (tied), or swept over an interval (range). `group` is meaningless
 *  unless the mode is 'tied'; min/max/step unless it is 'range'.
 *
 *  `whenName`/`whenValues` mark a parameter that only applies in company —
 *  a draft-step count means nothing with speculation off. Left empty, the
 *  parameter is always active. */
interface Row {
  name: string
  mode: 'fixed' | 'sweep' | 'tied' | 'range'
  fixed: string
  values: string[]
  group: number
  min: string
  max: string
  step: string
  whenName: string
  whenValues: string[]
}

function blankRow(name: string, partial: Partial<Row> = {}): Row {
  return {
    name, mode: 'fixed', fixed: '', values: [], group: 1,
    min: '', max: '', step: '', whenName: '', whenValues: [],
    ...partial,
  }
}

const form = ref({ name: '', engine: 'sglang', description: '', rows: [] as Row[] })
const addName = ref('')

const tunable = computed(() => catalog.value.filter((p) => p.tunable))
const advanced = computed(() => catalog.value.filter((p) => !p.tunable))
const used = computed(() => new Set(form.value.rows.map((r) => r.name)))

function spec(name: string): CatalogParam | undefined {
  return catalog.value.find((p) => p.name === name)
}

async function loadCatalog() {
  const { data } = await api.get('/search-spaces/catalog', {
    params: { engine: form.value.engine },
  })
  catalog.value = data.parameters
}

async function load() {
  spaces.value = (await api.get('/search-spaces')).data
}

function addRow(name: string) {
  const clean = name.trim()
  if (!clean || used.value.has(clean)) return
  const s = spec(clean)
  form.value.rows.push(blankRow(clean, {
    // A parameter with an example sweep is one worth sweeping — start it there.
    mode: s?.example ? 'sweep' : 'fixed',
    values: s?.example ? s.example.map(String) : [],
    // Seed the interval from the catalog's own bounds, so switching a row to
    // Range is not a blank form someone has to go and look the limits up for.
    min: s?.min != null ? String(s.min) : '',
    max: s?.max != null ? String(s.max) : '',
  }))
  addName.value = ''
}

/** Parameters this row could be gated on: any other row in the space. A
 *  condition on something the space never sets would drop the parameter from
 *  every candidate, so the backend rejects it — offer only what exists. */
function gateChoices(row: Row) {
  return form.value.rows.filter((r) => r.name !== row.name).map((r) => r.name)
}

/** Values the gating parameter can take, for the "only when" picker. */
function gateValues(name: string): string[] {
  const row = form.value.rows.find((r) => r.name === name)
  if (!row) return []
  if (row.mode === 'fixed') return row.fixed ? [row.fixed] : []
  if (row.mode === 'range') return []
  return row.values
}

/** What an interval expands to, spelled out under the inputs. A step is easy
 *  to get wrong by an order of magnitude, and "17 values" says so immediately
 *  where "min 2048 max 32768 step 2048" does not. */
function rangeSummary(row: Row): string {
  const min = Number(row.min)
  const max = Number(row.max)
  const step = Number(row.step)
  if (!row.step) return 'a step is required — it is what makes the space countable'
  if (!Number.isFinite(min) || !Number.isFinite(max) || !Number.isFinite(step)) {
    return 'min, max and step must be numbers'
  }
  if (step <= 0) return 'step must be positive'
  if (max < min) return 'max is below min'
  const count = Math.floor((max - min) / step + 1e-9) + 1
  const first = [0, 1, 2].filter((i) => i < count).map((i) => min + i * step)
  return `${count} value(s): ${first.join(', ')}${count > first.length ? ', …' : ''}`
}

/** Groups that already have a member, plus the next free one — so tying a
 *  second parameter to an existing pair is one click, and starting a separate
 *  pair is also one click. */
const groupChoices = computed(() => {
  const used = [...new Set(form.value.rows.filter((r) => r.mode === 'tied').map((r) => r.group))]
  used.sort((a, b) => a - b)
  return [...used, (used[used.length - 1] ?? 0) + 1]
})

/** Switching a row to 'tied' with no group in play would otherwise leave it in
 *  a group of one, which is a grid axis wearing a different hat. */
function onModeChange(row: Row) {
  if (row.mode === 'tied' && !groupChoices.value.includes(row.group)) {
    row.group = groupChoices.value[0]
  }
}

function removeRow(index: number) {
  form.value.rows.splice(index, 1)
}

/** Rows -> the {base, grid, tied, range, conditions} shape the platform
 *  consumes. */
function toSpace() {
  const base: Record<string, unknown> = {}
  const grid: Record<string, unknown[]> = {}
  const range: Record<string, Record<string, unknown>> = {}
  const conditions: Record<string, Record<string, unknown[]>> = {}
  const groups = new Map<number, Record<string, unknown[]>>()
  for (const row of form.value.rows) {
    const values = row.values.filter((v) => String(v).trim() !== '')
    if (row.mode === 'sweep') {
      if (values.length) grid[row.name] = values
    } else if (row.mode === 'tied') {
      if (values.length) {
        const group = groups.get(row.group) ?? {}
        group[row.name] = values
        groups.set(row.group, group)
      }
    } else if (row.mode === 'range') {
      // Sent even when incomplete: the backend's own validation is what tells
      // the author a step is missing, and hiding a half-filled range here
      // would show "1 candidate" instead of that message.
      const spec: Record<string, unknown> = {}
      if (String(row.min).trim() !== '') spec.min = row.min
      if (String(row.max).trim() !== '') spec.max = row.max
      if (String(row.step).trim() !== '') spec.step = row.step
      if (Object.keys(spec).length) range[row.name] = spec
    } else if (String(row.fixed).trim() !== '') {
      base[row.name] = row.fixed
    }
    const gates = row.whenValues.filter((v) => String(v).trim() !== '')
    if (row.whenName && gates.length) conditions[row.name] = { [row.whenName]: gates }
  }
  const tied = [...groups.entries()].sort((a, b) => a[0] - b[0]).map(([, g]) => g)
  return { name: form.value.name, engine: form.value.engine,
           description: form.value.description, base, grid, tied, range, conditions }
}

/** The space as it stands in the editor, in the shape everything else reads. */
const draftSpace = computed(() => toSpace())

/** A tied group whose columns are different lengths is zipped to the shortest,
 *  so the tail is silently dropped. That is worth saying out loud — the
 *  pairing is the whole point of tying. */
const ragged = computed(() =>
  draftSpace.value.tied
    .filter((group) => {
      const lengths = Object.keys(group).map((k) => group[k].length)
      return new Set(lengths).size > 1
    })
    .map((group) => Object.keys(group).join(' + ')),
)

/** Counted by the backend that will actually expand the space — the number
 *  shown here and the candidates a campaign creates are one derivation.
 *  null while it is unknown: "0 candidates" is a claim, and a preview that
 *  failed has not earned it. */
const candidateCount = computed<number | null>(() => preview.value?.candidate_count ?? null)

async function refreshPreview() {
  try {
    preview.value = (await api.post('/search-spaces/preview', toSpace())).data
  } catch {
    preview.value = null
  }
}
watch(() => JSON.stringify(toSpace()), refreshPreview)
watch(() => form.value.engine, loadCatalog)

function openNew() {
  editingId.value = null
  form.value = { name: '', engine: 'sglang', description: '', rows: [] }
  preview.value = null
  preview.value = null
  editorOpen.value = true
  // Ask for the count straight away: the watcher below only fires on a change,
  // and an empty space is one candidate, not none.
  loadCatalog().then(refreshPreview)
}

function openEdit(space: SearchSpace) {
  editingId.value = space.id
  form.value = {
    name: space.name,
    engine: space.engine,
    description: space.description,
    rows: rowsFrom(space.base, space.grid, space.tied ?? [], space.range ?? {},
                   space.conditions ?? {}),
  }
  editorOpen.value = true
  loadCatalog().then(refreshPreview)
}

/** The stored shape -> editable rows. One reader for both the saved space and
 *  a pasted YAML, so an import cannot end up meaning something the editor
 *  would not have produced. */
function rowsFrom(
  base: Record<string, unknown>,
  grid: Record<string, unknown[]>,
  tied: Record<string, unknown[]>[],
  range: Record<string, Record<string, unknown>> = {},
  conditions: Record<string, Record<string, unknown>> = {},
): Row[] {
  const rows: Row[] = [
    ...Object.entries(base ?? {}).map(([name, value]) =>
      blankRow(name, { mode: 'fixed', fixed: String(value) })),
    ...Object.entries(grid ?? {}).map(([name, values]) =>
      blankRow(name, {
        mode: 'sweep',
        values: (Array.isArray(values) ? values : [values]).map(String),
      })),
    ...Object.entries(range ?? {}).map(([name, spec]) =>
      blankRow(name, {
        mode: 'range',
        min: spec?.min != null ? String(spec.min) : '',
        max: spec?.max != null ? String(spec.max) : '',
        step: spec?.step != null ? String(spec.step) : '',
      })),
  ]
  ;(tied ?? []).forEach((group, index) => {
    for (const [name, values] of Object.entries(group ?? {})) {
      rows.push(blankRow(name, {
        mode: 'tied',
        values: (Array.isArray(values) ? values : [values]).map(String),
        group: index + 1,
      }))
    }
  })
  // Conditions live on the row they gate, so a parameter and the reason it is
  // sometimes absent are edited in one place.
  for (const [name, clause] of Object.entries(conditions ?? {})) {
    const row = rows.find((r) => r.name === name)
    const [gate, values] = Object.entries(clause ?? {})[0] ?? []
    if (row && gate) {
      row.whenName = gate
      row.whenValues = (Array.isArray(values) ? values : [values]).map(String)
    }
  }
  return rows
}

async function save() {
  const body = toSpace()
  if (!body.name.trim()) {
    ElMessage.error('Give the search space a name')
    return
  }
  busy.value = true
  try {
    if (editingId.value) await api.put(`/search-spaces/${editingId.value}`, body)
    else await api.post('/search-spaces', body)
    editorOpen.value = false
    await load()
    ElMessage.success('Saved')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Save failed')
  } finally {
    busy.value = false
  }
}

async function remove(space: SearchSpace) {
  try {
    await ElMessageBox.confirm(
      `Delete "${space.name}"? Campaigns already created from it are unaffected — ` +
        `they copied the space when they were created.`,
      'Delete search space',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  await api.delete(`/search-spaces/${space.id}`)
  await load()
}

const ioOpen = ref(false)
const ioText = ref('')

/** Export the space as YAML — pasteable into chat, a ticket, or another
 *  deployment. Round-trips through importYaml below. */
function openExport() {
  const space = toSpace()
  ioText.value = toYaml({
    name: space.name || 'unnamed',
    engine: space.engine,
    description: space.description,
    base: space.base,
    grid: space.grid,
    ...(space.tied.length ? { tied: space.tied } : {}),
    ...(Object.keys(space.range).length ? { range: space.range } : {}),
    ...(Object.keys(space.conditions).length ? { conditions: space.conditions } : {}),
  })
  ioOpen.value = true
}

function copyIo() {
  copyText(ioText.value, 'Search space copied')
}

/** Load YAML back into the editor rows. Accepts a full export (name/engine/
 *  base/grid) or just a bare {base, grid}. */
function importYaml() {
  let parsed: Record<string, any>
  try {
    parsed = fromYaml(ioText.value, 'Search space')
  } catch (error) {
    ElMessage.error(error instanceof YamlError ? error.message : 'Could not parse YAML')
    return
  }
  const base = (parsed.base ?? {}) as Record<string, unknown>
  const grid = (parsed.grid ?? {}) as Record<string, unknown[]>
  const tied = (parsed.tied ?? []) as Record<string, unknown[]>[]
  const range = (parsed.range ?? {}) as Record<string, Record<string, unknown>>
  const conditions = (parsed.conditions ?? {}) as Record<string, Record<string, unknown>>
  if (!Object.keys(base).length && !Object.keys(grid).length && !tied.length
      && !Object.keys(range).length) {
    ElMessage.error('No `base`, `grid`, `tied` or `range` found in that YAML')
    return
  }
  if (typeof parsed.name === 'string' && parsed.name) form.value.name = parsed.name
  if (parsed.engine === 'sglang' || parsed.engine === 'vllm') form.value.engine = parsed.engine
  if (typeof parsed.description === 'string') form.value.description = parsed.description
  form.value.rows = rowsFrom(base, grid, tied, range, conditions)
  ioOpen.value = false
  ElMessage.success(`Imported ${form.value.rows.length} parameter(s)`)
  loadCatalog().then(refreshPreview)
}

function summarize(space: SearchSpace): string {
  const dims = Object.entries(space.grid).map(([k, v]) => `${k}×${(v as unknown[]).length}`)
  for (const group of space.tied ?? []) {
    const keys = Object.keys(group)
    if (!keys.length) continue
    dims.push(`(${keys.join('+')})×${Math.min(...keys.map((k) => group[k].length))}`)
  }
  for (const [name, spec] of Object.entries(space.range ?? {})) {
    dims.push(`${name}∈[${spec.min}…${spec.max}]/${spec.step}`)
  }
  const gated = Object.keys(space.conditions ?? {}).length
  if (gated) dims.push(`${gated} conditional`)
  return dims.length ? dims.join(', ') : 'no sweep (single config)'
}

onMounted(() => {
  load()
  loadCatalog()
})
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">Search spaces</h1>
        <span class="muted">
          Reusable parameter grids — build one here, pick it when creating a campaign.
          <InfoHint :width="380">
            A campaign <b>copies</b> the space it names, so editing one here never changes a
            campaign that already ran.
            <br /><br />
            Parameters that only make sense together — a memory fraction that is safe at
            tp=4 but not at tp=1 — can be <b>tied</b>, so they move in step instead of being
            crossed.
          </InfoHint>
        </span>
      </div>
      <el-button type="primary" @click="openNew">New search space</el-button>
    </div>

    <el-table :data="spaces">
      <el-table-column label="Name" min-width="200">
        <template #default="{ row }">
          <div>{{ row.name }}</div>
          <!-- A note can run to a paragraph. One line here, the rest on hover:
               the list is for finding a space, not for reading its rationale. -->
          <el-tooltip v-if="row.description" placement="top" :show-after="200"
            :content="row.description">
            <div class="muted tiny note">{{ row.description }}</div>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="engine" label="Engine" width="90" />
      <el-table-column label="Sweep" min-width="260">
        <template #default="{ row }"><span class="mono">{{ summarize(row) }}</span></template>
      </el-table-column>
      <el-table-column label="Candidates" width="105">
        <template #default="{ row }">{{ row.candidate_count }}</template>
      </el-table-column>
      <el-table-column label="Updated" width="120">
        <template #default="{ row }">
          <span :title="exactTime(row.updated_at)">{{ relativeTime(row.updated_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="" width="140">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">Edit</el-button>
          <el-button size="small" type="danger" plain @click="remove(row)">Delete</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="editorOpen" :title="editingId ? 'Edit search space' : 'New search space'"
      width="900px" top="5vh">
      <el-form label-position="top">
        <div class="grid-3">
          <el-form-item label="Name">
            <el-input v-model="form.name" placeholder="e.g. qwen3.6 tp x mem-fraction" />
          </el-form-item>
          <el-form-item label="Engine">
            <el-select v-model="form.engine">
              <el-option label="sglang" value="sglang" />
              <el-option label="vllm" value="vllm" />
            </el-select>
          </el-form-item>
          <el-form-item label="Notes">
            <el-input v-model="form.description" placeholder="optional" />
          </el-form-item>
        </div>

        <el-form-item label="Add a parameter">
          <el-select v-model="addName" filterable allow-create placeholder="search parameters…"
            style="width: 100%" @change="addRow">
            <el-option-group label="Commonly tuned">
              <el-option v-for="p in tunable" :key="p.name" :value="p.name"
                :label="p.name" :disabled="used.has(p.name)">
                <span class="mono">{{ p.name }}</span>
                <span class="muted opt-help">{{ p.help }}</span>
              </el-option>
            </el-option-group>
            <el-option-group label="Usually fixed">
              <el-option v-for="p in advanced" :key="p.name" :value="p.name"
                :label="p.name" :disabled="used.has(p.name)">
                <span class="mono">{{ p.name }}</span>
                <span class="muted opt-help">{{ p.help }}</span>
              </el-option>
            </el-option-group>
          </el-select>
        </el-form-item>

        <el-table :data="form.rows" size="small" empty-text="No parameters yet">
          <el-table-column label="Parameter" min-width="200">
            <template #default="{ row }">
              <div class="mono">{{ row.name }}</div>
              <div class="muted tiny">{{ spec(row.name)?.help ?? 'custom parameter' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="Mode" width="250">
            <template #default="{ row }">
              <el-radio-group v-model="row.mode" size="small" @change="onModeChange(row)">
                <el-radio-button value="fixed">Fixed</el-radio-button>
                <el-radio-button value="sweep">Sweep</el-radio-button>
                <el-radio-button value="range">Range</el-radio-button>
                <el-radio-button value="tied">Tied</el-radio-button>
              </el-radio-group>
              <div v-if="row.mode === 'tied'" class="group-pick">
                <span class="muted tiny">moves with</span>
                <el-select v-model="row.group" size="small" style="width: 96px">
                  <el-option v-for="g in groupChoices" :key="g" :value="g"
                    :label="`group ${g}`" />
                </el-select>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="Value(s)" min-width="330">
            <template #default="{ row }">
              <template v-if="row.mode === 'fixed'">
                <el-select v-if="spec(row.name)?.choices" v-model="row.fixed" style="width: 100%">
                  <el-option v-for="c in spec(row.name)!.choices" :key="String(c)"
                    :label="String(c)" :value="String(c)" />
                </el-select>
                <el-switch v-else-if="spec(row.name)?.type === 'bool'" v-model="row.fixed"
                  active-value="true" inactive-value="false" />
                <el-input v-else v-model="row.fixed" class="mono" placeholder="value" />
              </template>
              <template v-else-if="row.mode === 'range'">
                <div class="range-row">
                  <el-input v-model="row.min" class="mono" size="small" placeholder="min" />
                  <span class="muted tiny">to</span>
                  <el-input v-model="row.max" class="mono" size="small" placeholder="max" />
                  <span class="muted tiny">step</span>
                  <el-input v-model="row.step" class="mono" size="small" placeholder="step" />
                </div>
                <div class="muted tiny">
                  {{ rangeSummary(row) }}
                </div>
              </template>
              <el-select v-else v-model="row.values" multiple filterable allow-create
                :reserve-keyword="false" style="width: 100%"
                :placeholder="row.mode === 'tied'
                  ? 'one value per row of the group, in order'
                  : 'type a value and press Enter, once per point'">
                <el-option v-for="c in spec(row.name)?.choices ?? []" :key="String(c)"
                  :label="String(c)" :value="String(c)" />
              </el-select>

              <div class="when-row">
                <span class="muted tiny">only when</span>
                <el-select v-model="row.whenName" size="small" clearable placeholder="always"
                  style="width: 190px">
                  <el-option v-for="g in gateChoices(row)" :key="g" :value="g" :label="g" />
                </el-select>
                <el-select v-if="row.whenName" v-model="row.whenValues" size="small" multiple
                  filterable allow-create :reserve-keyword="false" placeholder="is…"
                  style="width: 200px">
                  <el-option v-for="v in gateValues(row.whenName)" :key="v" :value="v"
                    :label="v" />
                </el-select>
              </div>
            </template>
          </el-table-column>
          <el-table-column width="60">
            <template #default="{ $index }">
              <el-button size="small" text type="danger" @click="removeRow($index)">✕</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="preview-head">
          <h4>What this sweeps</h4>
          <span v-if="candidateCount !== null" class="muted tiny">
            ≈ {{ Math.round((candidateCount * 17) / 6) / 10 }} h one at a time, at ~17 min
            per run
          </span>
        </div>
        <SpaceMap :space="draftSpace" compact :candidates="candidateCount" />

        <div v-if="ragged.length" class="ragged tiny">
          <div v-for="(keys, i) in ragged" :key="i">
            <b>{{ keys }}</b> — tied columns have different lengths; the unpaired tail is
            dropped.
          </div>
        </div>
        <div v-if="preview?.errors?.length" class="ragged tiny">
          <div v-for="(problem, i) in preview.errors" :key="i">{{ problem }}</div>
        </div>

        <div v-if="preview?.invalid?.length" class="muted tiny invalid">
          <b class="rejected">{{ preview.invalid.length }} rejected by static validation</b>
          <div v-for="(bad, i) in preview.invalid.slice(0, 4)" :key="i">
            <span class="mono">{{ JSON.stringify(bad.config) }}</span> — {{ bad.error }}
          </div>
          <div>(against the largest registered machine: {{ preview.gpu_count_used }} GPUs)</div>
        </div>

        <el-collapse class="raw">
          <el-collapse-item title="As YAML — what the campaign receives">
            <pre class="mono block">{{ toYaml({
              base: draftSpace.base,
              grid: draftSpace.grid,
              ...(draftSpace.tied.length ? { tied: draftSpace.tied } : {}),
              ...(Object.keys(draftSpace.range).length ? { range: draftSpace.range } : {}),
              ...(Object.keys(draftSpace.conditions).length
                ? { conditions: draftSpace.conditions } : {}),
            }) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </el-form>

      <template #footer>
        <el-button @click="openExport">Import / export YAML</el-button>
        <el-button @click="editorOpen = false">Cancel</el-button>
        <el-button type="primary" :loading="busy" @click="save">Save</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="ioOpen" title="Search space as YAML" width="640px">
      <p class="muted" style="margin-top: 0">
        Edit or paste a space here — <b>Import</b> loads it back into the editor.
        Accepts a full export or just <span class="mono">base:</span> /
        <span class="mono">grid:</span> / <span class="mono">tied:</span>.
      </p>
      <el-input v-model="ioText" type="textarea" :rows="16" class="mono" />
      <template #footer>
        <el-button @click="copyIo">Copy</el-button>
        <el-button @click="ioOpen = false">Close</el-button>
        <el-button type="primary" @click="importYaml">Import</el-button>
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
  grid-template-columns: 2fr 1fr 2fr;
  gap: 16px;
}
.opt-help {
  margin-left: 10px;
  font-size: 12px;
}
.tiny {
  font-size: 11.5px;
}
.note {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
}
.preview-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 18px 0 6px;
}
.preview-head h4 {
  margin: 0;
  font-size: 13px;
}
.rejected {
  color: var(--el-color-danger);
}
.range-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.range-row .el-input {
  width: 90px;
}
.when-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.group-pick {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.ragged {
  color: var(--el-color-danger);
  margin-top: 6px;
}
.invalid {
  background: #fef6f6;
  border-radius: 6px;
  padding: 8px 10px;
}
.raw {
  margin-top: 12px;
}
.block {
  background: #f1f5f9;
  padding: 12px;
  border-radius: 6px;
  max-height: 240px;
  overflow: auto;
}
</style>
