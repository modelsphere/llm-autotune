<script setup lang="ts">
/** Building a campaign, five decisions at a time.
 *
 *  This was one dialog with nineteen controls in it, which asked the author to
 *  hold the whole thing in their head at once and gave no signal about which
 *  parts they had actually finished. The steps here are the questions a
 *  campaign really answers — what to serve, where, what to try, what counts as
 *  better, and when — and the panel on the right is the answer so far, visible
 *  the entire time rather than at a review screen nobody reads.
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch, type Ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  api,
  type DatasetProfile,
  type Machine,
  type MachineGroup,
  type Objective,
  type Policy,
  type SearchSpace,
} from '../api/client'
import DeployBranchSelect from '../components/DeployBranchSelect.vue'
import InfoHint from '../components/InfoHint.vue'
import NightlyWindow from '../components/NightlyWindow.vue'
import SpaceMap from '../components/SpaceMap.vue'
import { campaignFromYaml } from '../utils/campaignYaml'
import { sweptKeysOf } from '../utils/space'
import { fromYaml, toYaml, YamlError } from '../utils/yaml'

const router = useRouter()
const step = ref(0)
const busy = ref(false)

const machines = ref<Machine[]>([])
/** Node groups that could carry this campaign's runs as one gang. */
const groups = ref<MachineGroup[]>([])
const searchSpaces = ref<SearchSpace[]>([])
const objectives = ref<Objective[]>([])
const datasetProfiles = ref<DatasetProfile[]>([])
/** Registered policy containers. Picking one in the Strategy select makes this
 *  a policy-as-code campaign: the container searches, the platform judges. */
const policies = ref<Policy[]>([])
const selectedSpaceId = ref<number | null>(null)
const selectedObjectiveId = ref<number | null>(null)
const verifyObjectiveId = ref<number | null>(null)
// The two-stage split is an opt-in disclosure, collapsed until someone opens it.
const advancedNames = ref<string[]>([])

const form = ref({
  name: '',
  engine: 'sglang',
  image: '',
  model_path: '',
  served_model_name: '',
  extras_text: toYaml({ env: {}, volumes: {} }),
  machine_names: [] as string[],
  // A node group each run deploys ACROSS. '' = single-node. Setting it makes
  // the group's members the pin; the Machines select is ignored.
  node_group: '',
  service_port: 28200,
  share_machine: true,
  max_run_minutes: 150,
  benchmark_slug: '',
  // A built-in planner name, or `policy:<id>` for an external policy container.
  planner: 'grid',
  // Only sent for a policy campaign — the budget the platform holds it to.
  policy_max_contenders: 1,
  policy_approx_minutes_each: 30,
  policy_model_startup_minutes: 5,
  confirm_top_k: 0,
  confirm_repeats: 3,
  // The expensive second stage. Off by default: it is only worth turning on
  // once there is a benchmark that replays real traffic to point it at.
  verify_enabled: false,
  verify_benchmark_slug: '',
  verify_top_k: 2,
  verify_max_run_minutes: 180,
  // The traffic sample the replay stage is held to for this campaign's whole
  // life. Empty = whatever the benchmark resolves each time it submits, which
  // is fine for one night and not for several.
  dataset_profile: '',
  dataset_policy: 'rebuild_at_start',
  // Which release branch of the deploy repo this campaign's winner is proposed
  // onto. The repo keeps one per model x card x engine, so it is a choice —
  // empty means the branch the bound baseline already tracks.
  deploy_branch: '',
  // Open the winner's merge request the moment the campaign finishes. Off by
  // default: a proposal that appears while nobody is watching is only welcome
  // when it was asked for.
  auto_promote: false,
  run_baseline_canary: true,
  daily_start: '23:00',
  daily_end: '08:00',
  schedule_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  schedule_until: null as string | null,
})

const STEPS = [
  { title: 'Model', hint: 'what to serve' },
  { title: 'Machines', hint: 'where it runs' },
  { title: 'Search', hint: 'what to try' },
  { title: 'Goal', hint: 'the tests, and what wins' },
  { title: 'Schedule', hint: 'when' },
  { title: 'Check', hint: 'before committing a night' },
]

const selectedSpace = computed(
  () => searchSpaces.value.find((s) => s.id === selectedSpaceId.value) ?? null)
const selectedObjective = computed(
  () => objectives.value.find((o) => o.id === selectedObjectiveId.value) ?? null)
const selectedVerifyObjective = computed(
  () => objectives.value.find((o) => o.id === verifyObjectiveId.value) ?? null)

/** On only when both halves are set — the API refuses either alone. */
const staged = computed(
  () => form.value.verify_enabled && form.value.verify_benchmark_slug.trim().length > 0)

/** The policy as a checkbox: two words beat a dropdown reading
 *  "rebuild_at_start / use_current". */
const rebuildAtStart = computed({
  get: () => form.value.dataset_policy !== 'use_current',
  set: (on: boolean) => {
    form.value.dataset_policy = on ? 'rebuild_at_start' : 'use_current'
  },
})

/** A profile on somebody else's clock. Pinning one still records what was
 *  measured, but nothing stops the build being replaced underneath the
 *  campaign — which is the whole problem the managed profile exists to solve.
 *  Unknown names (the list did not load, or it was typed) are not accused. */
const unmanagedProfile = computed(() => {
  const chosen = datasetProfiles.value.find((p) => p.name === form.value.dataset_profile)
  return chosen ? !chosen.managed : false
})

/** Empty means "use the platform default", which is a replay metric. Sending
 *  the screening objective here instead would score every replay run None. */
const verifyObjectivePayload = computed(() => {
  const chosen = selectedVerifyObjective.value
  if (!staged.value || !chosen) return {}
  return {
    name: chosen.name,
    target_metric: chosen.target_metric,
    direction: chosen.direction,
    redlines: chosen.redlines,
  }
})

/** The screening benchmark's metrics do not exist in a replay result, so an
 *  objective built on them scores every verification run None — which reads,
 *  in the morning, as "the expensive stage measured nothing". */
const verifyObjectiveMismatch = computed(() => {
  const target = selectedVerifyObjective.value?.target_metric ?? ''
  return target.startsWith('perf_guidellm')
})

/** What each step still needs, in the words of the thing that is missing.
 *  Shown on the step itself rather than only on submit, so nobody reaches the
 *  end and is told the first page was wrong. */
const problems = computed<string[][]>(() => [
  [
    !form.value.name.trim() && 'a name',
    !form.value.image.trim() && 'a container image',
    !form.value.model_path.trim() && 'the model path on the machine',
    !form.value.served_model_name.trim() && 'the served model name',
  ].filter(Boolean) as string[],
  [] as string[], // machines may be left empty (= any), so nothing is required
  [!selectedSpace.value && 'a search space'].filter(Boolean) as string[],
  [
    !selectedObjective.value && 'an objective',
    // Ticked but blank is the quiet failure: the campaign saves, the second
    // stage never fires, and nothing anywhere says why.
    form.value.verify_enabled && !form.value.verify_benchmark_slug.trim() &&
      'the benchmark to re-test the best on',
  ].filter(Boolean) as string[],
  [] as string[],
  [] as string[], // the check step reports; it does not block by itself
])

// -- preflight ---------------------------------------------------------------

interface PreflightCheck {
  key: string
  label: string
  status: 'pass' | 'warn' | 'fail' | 'skip'
  detail: string
  hint: string
}
interface PreflightMachine {
  machine: string
  ok: boolean
  failed: number
  warnings: number
  checks: PreflightCheck[]
}

const checking = ref(false)
const preflight = ref<{ machines: PreflightMachine[]; ok: boolean; note: string } | null>(null)

/** Ask the machines themselves, rather than guessing from the form. Every
 *  check here costs seconds and prevents a failure that costs a night: a
 *  missing model path is only discovered after the image pulls and the engine
 *  starts opening weights — twenty minutes in, on a box whose production
 *  service has already been torn down to make room. */
async function runPreflight() {
  if (!selectedSpace.value) return
  checking.value = true
  preflight.value = null
  try {
    const extras = fromYaml<{ volumes?: Record<string, string> }>(
      form.value.extras_text, 'Launch extras')
    const { data } = await api.post('/campaigns/preflight', {
      engine: form.value.engine,
      image: form.value.image.trim(),
      model_path: form.value.model_path,
      served_model_name: form.value.served_model_name,
      machine_names: form.value.machine_names,
      node_group: form.value.node_group,
      service_port: form.value.service_port,
      extra_volumes: extras.volumes ?? {},
      search_space: {
        base: selectedSpace.value.base,
        grid: selectedSpace.value.grid,
        tied: selectedSpace.value.tied ?? [],
        range: selectedSpace.value.range ?? {},
        conditions: selectedSpace.value.conditions ?? {},
      },
      // So the check can say whether each benchmark exists and can report what
      // its objective ranks on — a slug typo is otherwise a night spent
      // producing runs that score nothing.
      benchmark_slug: form.value.benchmark_slug.trim(),
      objective: selectedObjective.value
        ? { target_metric: selectedObjective.value.target_metric }
        : {},
      verify_benchmark_slug: staged.value ? form.value.verify_benchmark_slug.trim() : '',
      verify_objective: verifyObjectivePayload.value,
      // The dataset pins the replay stage, which by default is the primary
      // benchmark, so it is checked even when the split is off.
      dataset_profile: form.value.dataset_profile.trim(),
    })
    preflight.value = data
  } catch (error: any) {
    ElMessage.error(
      error instanceof YamlError ? error.message
        : (error.response?.data?.detail ?? 'Could not run the checks'),
    )
  } finally {
    checking.value = false
  }
}

const blockers = computed(() =>
  (preflight.value?.machines ?? []).flatMap((m) =>
    m.checks.filter((c) => c.status === 'fail').map((c) => ({ machine: m.machine, ...c })),
  ),
)

const statusLook: Record<string, { type: string; icon: string }> = {
  pass: { type: 'success', icon: '✓' },
  warn: { type: 'warning', icon: '!' },
  fail: { type: 'danger', icon: '✕' },
  skip: { type: 'info', icon: '–' },
}

/** Run the checks when the author arrives at the step — they are cheap, and a
 *  check nobody presses is a check nobody reads. */
watch(step, (i) => {
  if (i === STEPS.length - 1 && checkable.value && !preflight.value) runPreflight()
})

const canCreate = computed(() => problems.value.every((p) => p.length === 0))

/** What the machine-side probes need before they can say anything: an image to
 *  look for, a model path to stat, and a space to size the widest candidate
 *  from. Name and objective are not among them. */
const missingForCheck = computed(() =>
  [
    !form.value.image.trim() && 'the container image',
    !form.value.model_path.trim() && 'the model path',
    !selectedSpace.value && 'a search space',
  ].filter(Boolean) as string[])

const checkable = computed(() => missingForCheck.value.length === 0)

/** Steps the author has actually opened. A step with no required fields is
 *  valid from the start, so marking it complete before it has been seen tells
 *  someone they made a decision they have not made — the defaults may be fine,
 *  but that is theirs to confirm. */
const visited = ref(new Set<number>([0]))
watch(step, (i) => visited.value.add(i))

function stepStatus(i: number): 'process' | 'success' | 'error' | 'wait' {
  if (i === step.value) return 'process'
  if (!visited.value.has(i)) return 'wait'
  return problems.value[i].length ? 'error' : 'success'
}

/** The policy picked in the Strategy select, or null for a built-in planner. */
const selectedPolicy = computed<Policy | null>(() => {
  const m = /^policy:(\d+)$/.exec(form.value.planner)
  return m ? policies.value.find((p) => p.id === Number(m[1])) ?? null : null
})

async function load() {
  machines.value = (await api.get('/machines')).data
  try {
    groups.value = (await api.get('/machine-groups')).data
  } catch {
    groups.value = [] // the group picker just stays empty
  }
  try {
    policies.value = (await api.get('/policies')).data
  } catch {
    policies.value = [] // the select just offers the built-ins
  }
  searchSpaces.value = (await api.get('/search-spaces')).data
  objectives.value = (await api.get('/objectives')).data
  if (selectedObjectiveId.value === null) await selectDefaultObjective()
  // Last, and allowed to fail: it is the only one of these that leaves this
  // platform, and a benchmark platform that is briefly unreachable must not
  // stop anyone writing a campaign. The field falls back to free text.
  try {
    datasetProfiles.value = (await api.get('/campaigns/dataset-profiles')).data
  } catch {
    datasetProfiles.value = []
  }
}

/** The default campaign runs one benchmark — a replay — so its objective ranks
 *  on the replay score. */
const REPLAY_SCORE_METRIC = 'replay_prod.score_card_norm'

/** Preselect an objective rather than leaving it blank: a campaign created
 *  without one ranks on a fallback nobody chose. Prefer the replay score, since
 *  the default single benchmark is a replay; then the platform default; then the
 *  first saved objective. */
async function selectDefaultObjective() {
  let preferred = ''
  try {
    preferred = (await api.get('/objectives/metrics')).data.default_target_metric
  } catch {
    /* fall through to the platform default, then the first objective */
  }
  const pick =
    objectives.value.find((o) => o.target_metric === REPLAY_SCORE_METRIC) ??
    objectives.value.find((o) => o.is_builtin && o.target_metric === preferred) ??
    objectives.value[0]
  if (pick) selectedObjectiveId.value = pick.id
}

/** Picking a space fixes the engine — a vllm grid on an sglang campaign is not
 *  a thing the launcher can render. */
function applySearchSpace() {
  if (selectedSpace.value) form.value.engine = selectedSpace.value.engine
}

function openBuilder(path: string) {
  window.open(router.resolve(path).href, '_blank')
}

async function refreshLists() {
  searchSpaces.value = (await api.get('/search-spaces')).data
  objectives.value = (await api.get('/objectives')).data
  ElMessage.success('List refreshed')
}

/** The machines this campaign would actually land on. A node group is the pin
 *  when one is chosen — the Machines list is ignored then, so the fit hint and
 *  the preflight must read the group's members rather than an empty list that
 *  would silently mean "any machine". */
const pinnedNames = computed(() => {
  const group = groups.value.find((g) => g.name === form.value.node_group)
  return group ? group.members.map((m) => m.name) : form.value.machine_names
})

const machineCards = computed(() => {
  const pinned = pinnedNames.value
  const usable = machines.value.filter((m) => !pinned.length || pinned.includes(m.name))
  return Math.max(0, ...usable.map((m) => m.gpu_count))
})

/** Roughly how long the search takes, from the candidate count the backend
 *  already computed. Deliberately vague — a real night depends on how the
 *  configs pack — but the difference between "one night" and "a week" is the
 *  decision this screen exists to inform. */
const nightsNeeded = computed(() => {
  const count = selectedSpace.value?.candidate_count ?? 0
  if (!count) return ''
  const perRun = form.value.max_run_minutes > 60 ? 25 : 20
  const parallel = form.value.share_machine && machineCards.value >= 4 ? 4 : 1
  const hours = (count * perRun) / 60 / parallel
  const windowHours = windowLength.value || 9
  const nights = Math.max(1, Math.ceil(hours / windowHours))
  return `≈ ${hours.toFixed(1)} h of benchmarking — about ${nights} night${
    nights === 1 ? '' : 's'
  } at this window`
})

const windowLength = computed(() => {
  const parse = (s: string) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(s ?? '')
    return m ? Number(m[1]) + Number(m[2]) / 60 : null
  }
  const a = parse(form.value.daily_start)
  const b = parse(form.value.daily_end)
  if (a === null || b === null || a === b) return 0
  return b <= a ? 24 - a + b : b - a
})

// -- import ------------------------------------------------------------------

const importOpen = ref(false)
const importText = ref('')

/** A space or objective that came from a file rather than from the library.
 *
 *  The wizard picks saved rows by id, so an imported campaign — which carries
 *  its space and objective inline — needs something to select. Given a negative
 *  id it slots into the same lists and every downstream reader works unchanged,
 *  including the payload builder, which copies values rather than referencing
 *  the row. Matched to a saved row by name first, so importing something built
 *  here keeps pointing at the original. */
function adopt<T extends { id: number; name: string }>(
  list: Ref<T[]>, incoming: Record<string, unknown>, id: number, extra: Partial<T>,
): number {
  const name = String(incoming.name ?? '')
  const saved = name ? list.value.find((row: T) => row.name === name) : undefined
  if (saved) return saved.id
  const row = { ...extra, ...incoming, id, name: name || '(imported)' } as unknown as T
  list.value = [row, ...list.value.filter((r: T) => r.id !== id)]
  return id
}

function applyImport() {
  let parsed
  try {
    parsed = campaignFromYaml(importText.value)
  } catch (error: any) {
    ElMessage.error(error?.message ?? 'Could not read that YAML')
    return
  }

  // Scalars and maps land straight on the form; anything absent keeps the
  // default already there rather than being blanked.
  for (const [key, value] of Object.entries(parsed.fields)) {
    if (key === 'extra_env' || key === 'extra_volumes') continue
    if (key in form.value) (form.value as Record<string, unknown>)[key] = value
  }
  if (parsed.fields.extra_env || parsed.fields.extra_volumes) {
    form.value.extras_text = toYaml({
      env: parsed.fields.extra_env ?? {},
      volumes: parsed.fields.extra_volumes ?? {},
    })
  }

  if (parsed.search_space) {
    selectedSpaceId.value = adopt(searchSpaces, parsed.search_space, -1, {
      owner_id: 0, engine: form.value.engine, description: 'from the imported file',
      base: {}, grid: {}, tied: [], range: {}, conditions: {}, candidate_count: 0,
    } as any)
  }
  if (parsed.objective) {
    selectedObjectiveId.value = adopt(objectives, parsed.objective, -2, {
      owner_id: null, description: 'from the imported file', redlines: [],
      direction: 'maximize', is_builtin: false, created_at: '', updated_at: '',
    } as any)
  }
  if (parsed.verify_objective && Object.keys(parsed.verify_objective).length) {
    verifyObjectiveId.value = adopt(objectives, parsed.verify_objective, -3, {
      owner_id: null, description: 'from the imported file', redlines: [],
      direction: 'maximize', is_builtin: false, created_at: '', updated_at: '',
    } as any)
  }
  // Both halves or neither — the checkbox is the wizard's own state, not a
  // field in the file. Open the split disclosure when the file actually uses it,
  // so an imported two-stage campaign is not hidden behind a collapsed panel.
  form.value.verify_enabled = !!String(form.value.verify_benchmark_slug ?? '').trim()
  advancedNames.value = form.value.verify_enabled ? ['split'] : []

  importOpen.value = false
  importText.value = ''
  if (parsed.unknown.length) {
    ElMessage.warning(`Ignored unknown field(s): ${parsed.unknown.join(', ')}`)
  }

  // Straight to the checks: an imported campaign is one nobody has reviewed
  // against tonight's machines, and that is the whole point of this step.
  visited.value = new Set([0, 1, 2, 3, 4, 5])
  step.value = STEPS.length - 1
  ElMessage.success('Imported — running the pre-flight checks')
  runPreflight()
}

async function create() {
  if (!canCreate.value) {
    const firstBad = problems.value.findIndex((p) => p.length > 0)
    step.value = firstBad
    ElMessage.error(`Still needs ${problems.value[firstBad].join(', ')}`)
    return
  }
  busy.value = true
  try {
    const extras = fromYaml<{ env?: Record<string, string>; volumes?: Record<string, string> }>(
      form.value.extras_text, 'Launch extras')
    const { data } = await api.post('/campaigns', {
      name: form.value.name,
      engine: form.value.engine,
      image: form.value.image.trim(),
      model_path: form.value.model_path,
      served_model_name: form.value.served_model_name,
      extra_env: extras.env ?? {},
      extra_volumes: extras.volumes ?? {},
      machine_names: form.value.machine_names,
      node_group: form.value.node_group,
      service_port: form.value.service_port,
      share_machine: form.value.share_machine,
      max_run_minutes: form.value.max_run_minutes,
      benchmark_slug: form.value.benchmark_slug.trim(),
      // A policy campaign sends the built-in default as its planner: the API
      // ignores it once policy_id is set, and refuses an unknown name.
      planner: selectedPolicy.value ? 'grid' : form.value.planner,
      policy_id: selectedPolicy.value?.id ?? null,
      policy_settings: selectedPolicy.value
        ? {
            max_contenders: form.value.policy_max_contenders,
            approx_minutes_each: form.value.policy_approx_minutes_each,
            model_startup_minutes: form.value.policy_model_startup_minutes,
          }
        : {},
      confirm_top_k: form.value.confirm_top_k,
      confirm_repeats: form.value.confirm_repeats,
      run_baseline_canary: form.value.run_baseline_canary,
      daily_start: form.value.daily_start,
      daily_end: form.value.daily_end,
      schedule_timezone: form.value.schedule_timezone,
      schedule_until: form.value.schedule_until,
      // Copied, not referenced, and stamped with the name they came from:
      // editing the saved space or objective later must not re-rank or re-plan
      // a campaign that already ran.
      search_space: {
        name: selectedSpace.value!.name,
        base: selectedSpace.value!.base,
        grid: selectedSpace.value!.grid,
        tied: selectedSpace.value!.tied ?? [],
        range: selectedSpace.value!.range ?? {},
        conditions: selectedSpace.value!.conditions ?? {},
      },
      objective: {
        name: selectedObjective.value!.name,
        target_metric: selectedObjective.value!.target_metric,
        direction: selectedObjective.value!.direction,
        redlines: selectedObjective.value!.redlines,
      },
      // Both halves, or neither: the API refuses a slug with no top-k and a
      // top-k with no slug, because either one silently never fires.
      verify_benchmark_slug: staged.value ? form.value.verify_benchmark_slug.trim() : '',
      verify_top_k: staged.value ? form.value.verify_top_k : 0,
      verify_max_run_minutes: form.value.verify_max_run_minutes,
      verify_objective: verifyObjectivePayload.value,
      // Always sent, not gated on the split: the default single benchmark is a
      // replay, and a replay needs its dataset pinned to stay comparable.
      dataset_profile: form.value.dataset_profile.trim(),
      dataset_policy: form.value.dataset_policy,
      deploy_branch: form.value.deploy_branch.trim(),
      auto_promote: form.value.auto_promote,
    })
    router.push(`/campaigns/${data.id}`)
  } catch (error: any) {
    ElMessage.error(
      error instanceof YamlError
        ? error.message
        : (error.response?.data?.detail ?? 'Create failed'),
    )
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await load()
  // "Tune from this" on the Baselines page hands over the same YAML the Import
  // button takes; one-shot, so a reload of this page starts blank again.
  const prefill = sessionStorage.getItem('autotune_campaign_prefill')
  if (prefill) {
    sessionStorage.removeItem('autotune_campaign_prefill')
    importText.value = prefill
    applyImport()
  }
})
</script>

<template>
  <div class="page wide">
    <div class="header-row">
      <h1 class="page-title">New campaign</h1>
      <div>
        <el-button @click="importOpen = true">Import YAML</el-button>
        <el-button text @click="router.push('/campaigns')">Cancel</el-button>
      </div>
    </div>

    <el-steps :active="step" finish-status="success" align-center class="steps">
      <el-step v-for="(s, i) in STEPS" :key="s.title" :title="s.title" :description="s.hint"
        :status="stepStatus(i)" class="clickable-step" @click="step = i" />
    </el-steps>

    <div class="split">
      <div class="panel">
        <!-- 1. Model -->
        <template v-if="step === 0">
          <el-form label-position="top">
            <el-form-item label="Campaign name">
              <el-input v-model="form.name" placeholder="node-24 prefill sweep, week 32" />
            </el-form-item>
            <div class="grid-2">
              <el-form-item label="Engine">
                <el-select v-model="form.engine" style="width: 100%">
                  <el-option label="sglang" value="sglang" />
                  <el-option label="vllm" value="vllm" />
                </el-select>
              </el-form-item>
              <el-form-item label="Served model name">
                <el-input v-model="form.served_model_name" placeholder="glm-5" />
              </el-form-item>
            </div>
            <el-form-item label="Container image">
              <el-input v-model="form.image" class="mono"
                placeholder="lmsysorg/sglang:v0.5.13.post1" />
            </el-form-item>
            <el-form-item>
              <template #label>
                <span>Model path on the machine</span>
                <InfoHint>Bind-mounted into the container at <span class="mono">/model</span>.</InfoHint>
              </template>
              <el-input v-model="form.model_path" class="mono" placeholder="/data/models/qwen3.6" />
            </el-form-item>
            <el-form-item>
              <template #label>
                <span>Launch extras</span>
                <InfoHint>
                  YAML. Env vars and bind mounts every container gets — what the model needs
                  beyond the engine flags.
                </InfoHint>
              </template>
              <el-input v-model="form.extras_text" type="textarea" :rows="4" class="mono" />
            </el-form-item>
          </el-form>
        </template>

        <!-- 2. Machines -->
        <template v-if="step === 1">
          <el-form label-position="top">
            <el-form-item v-if="form.engine === 'sglang' && groups.length">
              <template #label>
                <span>Node group (multi-node)</span>
                <InfoHint :width="430">
                  Deploy each run <b>across</b> the group's members instead of on one
                  machine. Leave empty for an ordinary single-node campaign. The group's
                  members become the pin and the Machines list below is ignored.
                  <br /><br />
                  The member count is the deployment width: a 2-member group needs a config
                  whose tp×dp×pp divides evenly across the two boxes, and no other run may
                  share a member while the gang is live. Form the group on the Resources
                  page first.
                </InfoHint>
              </template>
              <el-select v-model="form.node_group" clearable style="width: 100%"
                placeholder="single node">
                <el-option v-for="g in groups" :key="g.id" :value="g.name"
                  :label="`${g.name} — ${g.node_count} node(s)${g.deployable ? '' : ' · blocked'}`" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <template #label>
                <span>Machines</span>
                <InfoHint>
                  Leave empty to allow any leased machine. Usually you want to pin — a model
                  and image belong to the box that has the weights on disk.
                </InfoHint>
              </template>
              <el-select v-model="form.machine_names" multiple style="width: 100%"
                :disabled="Boolean(form.node_group)"
                :placeholder="form.node_group ? 'the node group is the pin' : 'any leased machine'">
                <el-option v-for="m in machines" :key="m.id" :value="m.name"
                  :label="`${m.name} (${m.gpu_count}× ${m.gpu_type || 'GPU'})`" />
              </el-select>
            </el-form-item>
            <div class="grid-2">
              <el-form-item>
                <template #label>
                  <span>Engine port</span>
                  <InfoHint>
                    Avoid 30000–32767 on k8s nodes: kube-proxy hijacks that range on the node
                    IP, leaving the engine reachable only from localhost.
                  </InfoHint>
                </template>
                <el-input-number v-model="form.service_port" :min="1024" :max="65535" />
              </el-form-item>
              <el-form-item>
                <template #label>
                  <span>Max minutes per run</span>
                  <InfoHint>
                    A run past this is killed. Also the bound the platform promises a lease
                    holder when they ask for the machine back politely.
                  </InfoHint>
                </template>
                <el-input-number v-model="form.max_run_minutes" :min="10" :max="1440" />
              </el-form-item>
            </div>
            <el-form-item>
              <el-checkbox v-model="form.share_machine">
                Run several candidates at once, each pinned to its own GPUs
              </el-checkbox>
              <InfoHint :width="340">
                An 8-card node running one tp=2 config leaves six cards idle. Widest
                candidates are placed first. Turn off if a config is sensitive to
                host-level contention.
              </InfoHint>
            </el-form-item>
          </el-form>
        </template>

        <!-- 3. Search -->
        <template v-if="step === 2">
          <el-form label-position="top">
            <el-form-item>
              <template #label>
                <span>Search space</span>
                <el-button link type="primary" class="label-link"
                  @click="openBuilder('/search-spaces')">Build one</el-button>
                <el-button link class="label-link" @click="refreshLists">Refresh</el-button>
              </template>
              <el-select v-model="selectedSpaceId" filterable style="width: 100%"
                placeholder="pick a saved search space" @change="applySearchSpace">
                <el-option v-for="s in searchSpaces" :key="s.id" :value="s.id"
                  :label="`${s.name} (${s.engine})`">
                  <span>{{ s.name }}</span>
                  <span class="muted opt-help">
                    {{ s.engine }} · {{ sweptKeysOf(s).join(', ') || 'no sweep' }}
                  </span>
                </el-option>
              </el-select>
              <div v-if="!searchSpaces.length" class="warn hint">
                None saved yet — <b>Build one</b> opens the builder in a new tab.
              </div>
            </el-form-item>

            <SpaceMap v-if="selectedSpace" :space="selectedSpace" compact
              :candidates="selectedSpace.candidate_count" />

            <el-form-item class="spaced">
              <template #label>
                <span>Strategy</span>
                <InfoHint :width="360">
                  <b>Grid</b> is exhaustive and deterministic, which only works while the
                  space is small — it works through points in declaration order, so a night
                  that runs out of time stops wherever it stopped.
                  <b>Random</b> samples the whole space instead.
                  A <b>policy</b> is an external container (policy-as-code) that drives
                  its own search over this space; the platform launches engines,
                  benchmarks, and validates what it picks.
                </InfoHint>
              </template>
              <el-select v-model="form.planner" style="width: 100%" filterable>
                <el-option-group label="Built-in">
                  <el-option value="grid" label="Grid — every configuration, once" />
                  <el-option value="random" label="Random — sample until time runs out" />
                  <el-option value="tpe" label="TPE — Bayesian, learns from results as they land" />
                </el-option-group>
                <el-option-group v-if="policies.length" label="Policy containers">
                  <el-option v-for="p in policies" :key="p.id" :value="`policy:${p.id}`"
                    :label="`Policy — ${p.name}`">
                    <span>{{ p.name }}</span>
                    <span class="muted opt-help mono">{{ p.image }}</span>
                  </el-option>
                </el-option-group>
              </el-select>
            </el-form-item>

            <el-form-item v-if="selectedPolicy" class="spaced">
              <template #label>
                <span>Policy budget</span>
                <InfoHint :width="360">
                  How much of the window the platform reserves at the end to validate
                  the policy's contenders: each one costs about
                  <b>approx minutes + startup minutes</b>. A policy campaign needs a
                  window to activate — set a nightly schedule or a one-off window on the
                  next step, or use <b>Force start</b> on the campaign page.
                </InfoHint>
              </template>
              <div class="stack">
                <div class="sentence">
                  <span>Validate up to</span>
                  <el-input-number v-model="form.policy_max_contenders" :min="1" :max="8"
                    size="small" controls-position="right" class="inline-num" />
                  <span>contenders, about</span>
                  <el-input-number v-model="form.policy_approx_minutes_each" :min="5" :max="240"
                    size="small" controls-position="right" class="inline-num" />
                  <span>minutes each, plus</span>
                  <el-input-number v-model="form.policy_model_startup_minutes" :min="0" :max="120"
                    size="small" controls-position="right" class="inline-num" />
                  <span>minutes of model startup.</span>
                </div>
                <div class="muted tiny mono">{{ selectedPolicy.image }}</div>
              </div>
            </el-form-item>

            <el-form-item>
              <template #label>
                <span>Confirming the winner</span>
                <InfoHint :width="360">
                  One benchmark is a signal, not a decision. A config can be fastest on
                  average and still miss a redline on a second run — and on this rig the
                  run-to-run spread is about 0.25%, so a 0.5% win is not a win yet.
                </InfoHint>
              </template>
              <!-- The numbers sit inside the sentence they belong to. As two bare
                   spinners under "Confirm the best N, M times" nobody could tell
                   which box was N.
                   Wrapped in a block: el-form-item's content is a flex ROW, so
                   the sentence and its footnote were laid out side by side. -->
              <div class="stack">
                <div class="sentence">
                  <span>Before finishing, re-run the best</span>
                  <el-input-number v-model="form.confirm_top_k" :min="0" :max="10"
                    size="small" controls-position="right" class="inline-num" />
                  <span>configurations</span>
                  <el-input-number v-model="form.confirm_repeats" :min="2" :max="10"
                    size="small" controls-position="right" class="inline-num"
                    :disabled="form.confirm_top_k === 0" />
                  <span>times each.</span>
                </div>
                <div class="muted hint">
                  <template v-if="form.confirm_top_k === 0">
                    Off — whatever wins once, wins.
                  </template>
                  <template v-else>
                    Adds up to {{ form.confirm_top_k * (form.confirm_repeats - 1) }} extra
                    run(s) at the end, and reports each winner's spread instead of a single
                    number.
                  </template>
                </div>
              </div>
            </el-form-item>
          </el-form>
        </template>

        <!-- 4. Goal — the benchmark every candidate runs, and what wins.
             One benchmark by default, and that benchmark is the replay. The
             two-stage split — a cheaper screen on every candidate, the primary
             only on the best few — is an opt-in below, off until there is a
             reason to pay for it. -->
        <template v-if="step === 3">
          <el-form label-position="top">
            <div class="stage-box">
              <div class="stage-head">
                <h4>Benchmark</h4>
                <span class="muted tiny">every candidate runs this</span>
              </div>

              <div class="stage-fields stacked">
                <div class="stage-field">
                  <label class="muted tiny">Benchmark</label>
                  <el-input v-model="form.benchmark_slug" class="mono" size="small"
                    placeholder="rolling-replay-test-mf-v0" />
                </div>
                <div class="stage-field">
                  <label class="muted tiny">
                    Ranked by
                    <el-button link type="primary" class="label-link"
                      @click="openBuilder('/objectives')">Build one</el-button>
                    <el-button link class="label-link" @click="refreshLists">Refresh</el-button>
                  </label>
                  <el-select v-model="selectedObjectiveId" size="small" filterable
                    placeholder="pick a saved objective">
                    <el-option v-for="o in objectives" :key="o.id" :value="o.id"
                      :label="o.name">
                      <span>{{ o.name }}</span>
                      <span class="muted opt-help">
                        {{ o.direction }} {{ o.target_metric }}
                      </span>
                    </el-option>
                  </el-select>
                </div>
                <!-- The replay is held to one pinned traffic sample for the
                     campaign's whole life. It lives here, with the primary
                     benchmark, because by default that benchmark IS the replay
                     and needs it; in the split below it pins the replay stage. -->
                <div class="stage-field">
                  <label class="muted tiny">Dataset</label>
                  <el-select v-model="form.dataset_profile" size="small" clearable filterable
                    allow-create default-first-option class="mono"
                    placeholder="not pinned — whatever the benchmark resolves">
                    <el-option v-for="p in datasetProfiles" :key="p.name" :value="p.name"
                      :label="p.name">
                      <span class="mono">{{ p.name }}</span>
                      <span class="muted opt-help">
                        {{ p.managed ? 'managed' : 'rebuilds on its own' }}
                      </span>
                    </el-option>
                  </el-select>
                </div>
              </div>

              <template v-if="form.dataset_profile">
                <el-checkbox v-model="rebuildAtStart" size="small">
                  Rebuild it when the campaign starts
                </el-checkbox>
                <div v-if="unmanagedProfile" class="stage-warn">
                  <b class="mono">{{ form.dataset_profile }}</b> rebuilds on its own
                  schedule, so its build can be replaced mid-campaign. Every run after
                  that would be measured on different traffic and flagged as not
                  comparable.
                </div>
              </template>

              <div v-if="selectedObjective" class="goal-box">
                <div class="goal-line">
                  <span class="muted tiny lead-in">rank by</span>
                  <b>{{ selectedObjective.direction }}</b>
                  <span class="mono">{{ selectedObjective.target_metric }}</span>
                </div>
                <!-- "must hold", not "reject if": a redline is the condition a run
                     has to SATISFY. Labelled the other way round, `pass_rate >= 0.99`
                     read as "reject the runs that pass", which is the opposite of
                     what the ranker does with it. -->
                <div v-if="selectedObjective.redlines.length" class="goal-line">
                  <span class="muted tiny lead-in">must hold</span>
                  <span v-for="(r, i) in selectedObjective.redlines" :key="i"
                    class="mono chip">
                    {{ r.metric }} {{ r.op }} {{ r.value }}
                  </span>
                </div>
                <div v-else class="muted tiny">
                  no redlines — every successful run qualifies
                </div>
              </div>
            </div>

            <!-- The opt-in split, collapsed by default: screen every candidate
                 with a cheaper benchmark first, then run a second benchmark only
                 on the best few. Off until someone opens it. -->
            <el-collapse v-model="advancedNames" class="advanced">
              <el-collapse-item name="split">
                <template #title>
                  Two-stage: screen cheaply, then verify the best (optional)
                </template>

                <div class="stage-box" :class="{ off: !form.verify_enabled }">
                  <div class="stage-head">
                    <el-checkbox v-model="form.verify_enabled">
                      <h4>Screen first, verify the best</h4>
                    </el-checkbox>
                    <InfoHint :width="380">
                      A cheap benchmark (random tokens) cannot see prefix cache reuse at
                      all. Screen every candidate with it, then run a second benchmark —
                      a replay of recorded production, the better part of an hour per
                      config — only on the shortlist.
                    </InfoHint>
                  </div>

                  <template v-if="form.verify_enabled">
                    <!-- With the split on, the benchmark above shifts from "the one
                         measurement" to "the cheap screen"; say so, since the field
                         itself does not move. -->
                    <p class="muted tiny split-note">
                      The benchmark above now screens every candidate; the one below is
                      run only on the best few, and decides the winner.
                    </p>
                    <div class="stage-fields stacked">
                      <div class="stage-field">
                        <label class="muted tiny">Benchmark for the best few</label>
                        <el-input v-model="form.verify_benchmark_slug" class="mono" size="small"
                          placeholder="rolling-replay-test-mf-v0" />
                      </div>
                      <div class="stage-field">
                        <label class="muted tiny">Ranked by</label>
                        <el-select v-model="verifyObjectiveId" size="small" clearable filterable
                          placeholder="platform default — replay value per card">
                          <el-option v-for="o in objectives" :key="o.id" :value="o.id"
                            :label="o.name">
                            <span>{{ o.name }}</span>
                            <span class="muted opt-help">{{ o.target_metric }}</span>
                          </el-option>
                        </el-select>
                      </div>
                    </div>

                    <!-- Two short sentences rather than one long one: in a panel
                         this narrow, a single sentence wrapped around both spinners
                         and nobody could tell which number was which. -->
                    <div class="sentence">
                      <span>Run the best</span>
                      <el-input-number v-model="form.verify_top_k" :min="1" :max="10"
                        size="small" controls-position="right" class="inline-num" />
                      <span>configurations,</span>
                    </div>
                    <div class="sentence">
                      <span>allowing</span>
                      <el-input-number v-model="form.verify_max_run_minutes" :min="10" :max="1440"
                        :step="30" size="small" controls-position="right"
                        class="inline-num wide-num" />
                      <span>minutes each.</span>
                    </div>

                    <div v-if="verifyObjectiveMismatch" class="stage-warn">
                      <b>{{ selectedVerifyObjective?.target_metric }}</b> comes from the sweep,
                      not from a replay — a replay result does not contain it, so every
                      verified run would score nothing.
                    </div>
                    <div v-else class="muted tiny">
                      Adds {{ form.verify_top_k }} run(s) at the end, roughly
                      {{ ((form.verify_top_k * form.verify_max_run_minutes) / 60).toFixed(1) }} h.
                      This measurement decides the winner.
                    </div>
                  </template>
                  <div v-else class="muted tiny">
                    Off — one benchmark, and whatever wins it wins.
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>

            <!-- Where a winner ends up. Optional and easy to miss on purpose:
                 it changes nothing about the run, only which release branch the
                 merge request is opened against later. -->
            <el-collapse class="advanced">
              <el-collapse-item name="deploy">
                <template #title>Where the winner is proposed (optional)</template>
                <el-form-item label="Deploy branch">
                  <DeployBranchSelect v-model="form.deploy_branch" />
                  <div class="muted tiny">
                    The deploy repo keeps one release branch per model × card × engine.
                    Leave empty and a winner goes onto whichever branch the matching
                    baseline is bound to; name one here when this campaign tunes for a
                    different release line.
                  </div>
                </el-form-item>
                <el-form-item>
                  <el-checkbox v-model="form.auto_promote">
                    Submit the winner as a merge request automatically
                  </el-checkbox>
                  <InfoHint :width="380">
                    When the campaign finishes, the platform opens the merge request
                    itself — same diff, same ownership policy, same preview you would
                    have seen. It needs a baseline for this model bound to the deploy
                    repo, and it will not propose a run that crossed a redline. While
                    the platform is in dry-run it records the draft and writes nothing.
                  </InfoHint>
                </el-form-item>
              </el-collapse-item>
            </el-collapse>

            <el-form-item class="spaced">
              <el-checkbox v-model="form.run_baseline_canary">
                Benchmark production first, before clearing it
              </el-checkbox>
              <InfoHint :width="340">
                The night's control run: measures what production does today, on tonight's
                hardware, with this campaign's benchmark. Production is only torn down once
                this passes — a machine we could not measure is the one not to clear.
              </InfoHint>
            </el-form-item>
          </el-form>
        </template>

        <!-- 5. Schedule -->
        <template v-if="step === 4">
          <p class="muted lead">
            The campaign starts and stops itself. It wakes at the start time, works until
            the end time, and picks up where it left off the following night.
          </p>
          <NightlyWindow
            v-model:start="form.daily_start"
            v-model:end="form.daily_end"
            v-model:timezone="form.schedule_timezone"
            v-model:until="form.schedule_until" />
          <p v-if="nightsNeeded" class="estimate">{{ nightsNeeded }}</p>
          <el-alert v-if="!form.daily_start || !form.daily_end" type="info" :closable="false"
            show-icon class="spaced"
            title="No window means no automatic start"
            description="The campaign is created paused and runs only while you start it by
              hand — and nothing puts production back on its own." />
        </template>

        <!-- 6. Check -->
        <template v-if="step === 5">
          <div class="check-head">
            <p class="muted lead">
              Asked of the machines themselves. Each of these costs seconds now and a
              night to find out the other way.
            </p>
            <!-- Offered only when it would actually do something. The button used
                 to show on an empty form, where `runPreflight` returns before
                 making a single call — a control that looks live, does nothing,
                 and gives no reason. -->
            <el-button v-if="checkable" size="small" :loading="checking"
              @click="runPreflight">
              {{ preflight ? 'Re-run checks' : 'Run checks' }}
            </el-button>
          </div>

          <div v-if="checking" class="muted">Checking…</div>

          <el-alert v-else-if="preflight?.note" type="warning" :closable="false" show-icon
            :title="preflight.note" />

          <template v-else-if="preflight">
            <el-alert v-if="blockers.length" type="error" :closable="false" show-icon
              class="verdict"
              :title="`${blockers.length} problem(s) would stop this campaign`"
              description="You can still create it — but it will not run until these are
                fixed." />
            <el-alert v-else type="success" :closable="false" show-icon class="verdict"
              title="Nothing is in the way"
              description="Warnings below are worth reading; none of them block a run." />

            <div v-for="m in preflight.machines" :key="m.machine" class="machine-checks">
              <div class="machine-name mono">{{ m.machine }}</div>
              <!-- Findings that need a decision get a line each; passes get a
                   chip, so the report is visibly complete without burying the
                   two entries that matter under seven that do not. -->
              <div v-for="c in m.checks.filter((x) => x.status !== 'pass')" :key="c.key"
                class="check" :class="c.status">
                <span class="icon">{{ statusLook[c.status]?.icon ?? '?' }}</span>
                <div class="body">
                  <div><b>{{ c.label }}</b> — {{ c.detail }}</div>
                  <div v-if="c.hint" class="muted tiny">{{ c.hint }}</div>
                </div>
              </div>
              <div class="passed">
                <span v-for="c in m.checks.filter((x) => x.status === 'pass')" :key="c.key"
                  class="ok-chip" :title="c.detail">✓ {{ c.label }}</span>
              </div>
            </div>
          </template>

          <p v-else-if="!checkable" class="muted">
            Nothing to check yet — these ask a real machine about
            <b>{{ missingForCheck.join(', ') }}</b>. Fill that in and the checks run
            when you come back to this step.
          </p>
          <p v-else class="muted">Checks run automatically when you reach this step.</p>
        </template>
      </div>

      <!-- the answer so far, visible throughout rather than at a review screen -->
      <aside class="summary">
        <h3>So far</h3>
        <dl>
          <dt>Name</dt>
          <dd :class="{ empty: !form.name }">{{ form.name || 'unnamed' }}</dd>
          <dt>Serving</dt>
          <dd :class="{ empty: !form.served_model_name }">
            {{ form.served_model_name || '—' }} <span class="muted">on {{ form.engine }}</span>
          </dd>
          <dt>Machines</dt>
          <dd v-if="form.node_group">
            <el-tag size="small" type="primary" effect="plain">multi-node</el-tag>
            node group <b>{{ form.node_group }}</b>
            <span class="muted">
              ({{ groups.find((g) => g.name === form.node_group)?.node_count ?? '?' }} nodes)
            </span>
          </dd>
          <dd v-else>{{ form.machine_names.join(', ') || 'any leased machine' }}</dd>
          <dt>Search</dt>
          <dd :class="{ empty: !selectedSpace }">
            <template v-if="selectedSpace">
              {{ selectedSpace.name }}
              <span class="muted">· {{ selectedSpace.candidate_count }} candidates
                · {{ selectedPolicy ? `policy ${selectedPolicy.name}` : form.planner }}</span>
            </template>
            <template v-else>not chosen</template>
          </dd>
          <dt>Benchmark</dt>
          <dd :class="{ empty: !selectedObjective }">
            {{ selectedObjective?.name ?? 'no objective chosen' }}
            <span class="muted">on
              {{ form.benchmark_slug.trim() || 'a benchmark you name' }}</span>
          </dd>
          <dt>Two-stage</dt>
          <dd :class="{ empty: !staged }">
            <template v-if="staged">
              screen every candidate, then the best {{ form.verify_top_k }} on
              <span class="mono">{{ form.verify_benchmark_slug }}</span>
            </template>
            <template v-else>off — one benchmark decides</template>
          </dd>
          <dt>Window</dt>
          <dd :class="{ empty: !form.daily_start || !form.daily_end }">
            <template v-if="form.daily_start && form.daily_end">
              {{ form.daily_start }} → {{ form.daily_end }} nightly
            </template>
            <template v-else>manual</template>
          </dd>
        </dl>
        <p v-if="nightsNeeded" class="muted tiny">{{ nightsNeeded }}</p>
      </aside>
    </div>

    <el-dialog v-model="importOpen" title="Import a campaign" width="720px">
      <p class="muted lead">
        Paste the YAML exported from another campaign. It fills the steps in — nothing
        is created until you press Create, and the pre-flight checks run first.
      </p>
      <el-input v-model="importText" type="textarea" :rows="16" class="mono"
        placeholder="name: ...&#10;engine: sglang&#10;image: ..." />
      <template #footer>
        <el-button @click="importOpen = false">Cancel</el-button>
        <el-button type="primary" :disabled="!importText.trim()" @click="applyImport">
          Import and check
        </el-button>
      </template>
    </el-dialog>

    <div class="footer">
      <el-button :disabled="step === 0" @click="step--">Back</el-button>
      <span class="grow" />
      <span v-if="problems[step].length" class="muted tiny needs">
        needs {{ problems[step].join(', ') }}
      </span>
      <el-button v-if="step < STEPS.length - 1" type="primary" @click="step++">Next</el-button>
      <el-button v-else type="primary" :loading="busy" :disabled="!canCreate" @click="create">
        Create campaign
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.wide {
  max-width: 1100px;
}
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.steps {
  margin: 8px 0 22px;
}
.clickable-step {
  cursor: pointer;
}
.split {
  display: grid;
  grid-template-columns: 1fr 280px;
  gap: 24px;
  align-items: start;
}
.panel {
  background: #fff;
  border: 1px solid var(--autotune-border);
  border-radius: 8px;
  padding: 20px 22px 6px;
  min-height: 340px;
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.spaced {
  margin-top: 14px;
}
.lead {
  margin: 0 0 14px;
  line-height: 1.6;
}
.estimate {
  margin: 12px 0 0;
  font-size: 12.5px;
  color: var(--el-color-primary-dark-2);
}
.opt-help {
  margin-left: 10px;
  font-size: 12px;
}
.hint {
  font-size: 12px;
  margin-top: 4px;
}
.warn {
  color: var(--el-color-warning);
}
.label-link {
  margin-left: 12px;
  font-size: 12px;
  font-weight: 400;
}
.goal-box {
  background: var(--autotune-bg);
  border: 1px solid var(--autotune-border);
  border-radius: 6px;
  padding: 12px 14px;
}
/* Flex with an explicit gap rather than inline elements: Vue collapses the
   whitespace between them, which ran "rank by" straight into "maximize". */
.goal-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.goal-line:last-child {
  margin-bottom: 0;
}
.lead-in {
  min-width: 60px;
}
/* el-form-item lays its content out as a flex row, so anything with more than
   one child needs its own block wrapper or the pieces sit side by side. */
.stack {
  width: 100%;
}
.sentence {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  line-height: 1.9;
}
.inline-num {
  width: 92px;
}
.wide-num {
  width: 108px;
}
.stage-box {
  background: var(--autotune-bg);
  border: 1px solid var(--autotune-border);
  border-radius: 6px;
  padding: 12px 14px;
  margin-bottom: 18px;
}
/* A stage that is switched off still shows its heading, so the reader can see
   the campaign has a second stage available and chose not to use it. */
.stage-box.off {
  background: transparent;
}
.stage-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  /* A shared height keeps the checkbox-headed split box aligned with its own
     fields whether it is on or off. */
  min-height: 32px;
}
.stage-head h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}
.stage-box .goal-box {
  margin-top: 4px;
}
/* The opt-in split disclosure and its lead-in note. */
.advanced {
  margin: 4px 0 8px;
}
.split-note {
  margin: 0 0 10px;
  line-height: 1.6;
}
.stage-fields {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin: 10px 0 8px;
}
/* Inside a stage column there is only room for one field per row. The basis
   override matters: `flex: 1 1 240px` sizes the MAIN axis, which in a column
   is the height — every field grew to 240px tall and the box filled with gaps. */
.stage-fields.stacked {
  flex-direction: column;
  gap: 10px;
}
.stage-fields.stacked .stage-field {
  flex: 0 0 auto;
}
.stage-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1 1 240px;
}
.stage-warn {
  font-size: 12px;
  color: var(--el-color-warning);
  line-height: 1.6;
}
.check-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.verdict {
  margin-bottom: 14px;
}
.machine-checks {
  margin-bottom: 16px;
}
.machine-name {
  color: var(--autotune-muted);
  margin-bottom: 6px;
}
.check {
  display: flex;
  gap: 10px;
  padding: 5px 0;
  font-size: 13px;
  line-height: 1.55;
  border-top: 1px solid var(--autotune-border);
}
.check .icon {
  width: 16px;
  flex: none;
  text-align: center;
  font-weight: 700;
}
.check.pass .icon {
  color: var(--el-color-success);
}
.check.warn .icon {
  color: var(--el-color-warning);
}
.check.fail .icon {
  color: var(--el-color-danger);
}
.check.skip .icon {
  color: var(--autotune-muted);
}
.check .body {
  min-width: 0;
  word-break: break-word;
}
.passed {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 0 2px;
}
.ok-chip {
  font-size: 11.5px;
  line-height: 20px;
  padding: 0 7px;
  border-radius: 4px;
  background: #f0fdf4;
  color: #15803d;
  border: 1px solid #bbf7d0;
  cursor: help;
}
.chip {
  background: #fff;
  border: 1px solid var(--autotune-border);
  border-radius: 4px;
  padding: 0 6px;
}
.summary {
  background: #fff;
  border: 1px solid var(--autotune-border);
  border-radius: 8px;
  padding: 16px 18px;
  position: sticky;
  top: 16px;
}
.summary h3 {
  margin: 0 0 10px;
  font-size: 13px;
  color: var(--autotune-muted);
  font-weight: 600;
}
dl {
  margin: 0;
}
dt {
  font-size: 11.5px;
  color: var(--autotune-muted);
  margin-top: 10px;
}
dt:first-child {
  margin-top: 0;
}
dd {
  margin: 2px 0 0;
  font-size: 13px;
  word-break: break-word;
}
dd.empty {
  color: var(--autotune-muted);
  font-style: italic;
}
.tiny {
  font-size: 11.5px;
}
.footer {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 18px;
}
.grow {
  flex: 1;
}
.needs {
  color: var(--el-color-warning);
}
</style>
