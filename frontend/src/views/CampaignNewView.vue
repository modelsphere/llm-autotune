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
import { useRoute, useRouter } from 'vue-router'
import {
  api,
  type Baseline,
  type DatasetProfile,
  type Machine,
  type MachineGroup,
  type Objective,
  type Policy,
  type SearchSpace,
} from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import NightlyWindow from '../components/NightlyWindow.vue'
import SpaceMap from '../components/SpaceMap.vue'
import WorkloadPicker from '../components/WorkloadPicker.vue'
import {
  applyStrategy,
  describeStrategy,
  plugins,
  strategyFromExtensions,
  strategyValue,
  type Extensions,
} from '../plugins'
import { campaignFromYaml } from '../utils/campaignYaml'
import { sweptKeysOf } from '../utils/space'
import { defaultWorkload, specOf, type BenchmarkChoice, type Workload } from '../utils/workload'
import { fromYaml, toYaml, YamlError } from '../utils/yaml'

const router = useRouter()
const route = useRoute()
const step = ref(0)
const busy = ref(false)

const machines = ref<Machine[]>([])
/** Node groups that could carry this campaign's runs as one gang. */
const groups = ref<MachineGroup[]>([])
const searchSpaces = ref<SearchSpace[]>([])
const objectives = ref<Objective[]>([])
const datasetProfiles = ref<DatasetProfile[]>([])
/** What LLMBench will run a submission against, for "an existing benchmark".
 *  Empty when LLMBench is unreachable — the picker still takes a typed slug. */
const benchmarks = ref<BenchmarkChoice[]>([])
/** What every candidate is measured with, and (two-stage) what the best few
 *  are re-measured with. Described workloads become benchmarks on create. */
const workload = ref<Workload>(defaultWorkload('sweep'))
const verifyWorkload = ref<Workload>(defaultWorkload('replay'))
/** The LLMBench module each workload runs — the prefix of its metric names,
 *  which is what decides the objectives that can rank it. '' = unknown. */
const screenModule = ref('')
const verifyModule = ref('')
/** Registered policy containers. Picking one in the Strategy select makes this
 *  a policy-as-code campaign: the container searches, the platform judges. */
const policies = ref<Policy[]>([])
const selectedSpaceId = ref<number | null>(null)
const selectedObjectiveId = ref<number | null>(null)
const verifyObjectiveId = ref<number | null>(null)
// The settings most campaigns never touch, one disclosure per step, collapsed
// until opened. Separate lists: an el-collapse replaces its whole v-model, so a
// shared one would close every other step's section.
const advancedNames = ref<string[]>([]) // 'split' — the two-stage benchmark
const modelAdvanced = ref<string[]>([])
const machinesAdvanced = ref<string[]>([])
const searchAdvanced = ref<string[]>([])

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
  // Where the search for a free port starts; each run takes the next free one.
  service_port: 28200,
  share_machine: true,
  // A safety bound only: the window plans from what runs have actually taken.
  max_run_minutes: 720,
  benchmark_slug: '',
  // '' = no policy: the campaign tries every configuration in the space, in
  // order. `policy:<id>` = an external policy container searches it instead.
  // `plugin:<name>:<value>` = an installed plugin plans it (src/plugins).
  strategy: '',
  // What installed plugins keep about the campaign, by plugin name; the
  // Strategy select writes a plugin strategy into it on create.
  extensions: {} as Extensions,
  // Only sent for a policy campaign: how many of its finalists the platform
  // re-measures at the end. Their benchmark and startup time are learned.
  policy_max_contenders: 2,
  // Timings an imported campaign pinned, carried through untouched.
  policy_pinned: {} as Record<string, number>,
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

/** The name a campaign gets when its author leaves it blank. */
const defaultName = computed(() =>
  `${servedName.value || 'model'} · ${new Date().toISOString().slice(0, 10)}`)

/** What the engine serves the model as: the typed name, else the last part of
 *  the model path — the same rule the API applies to an empty field. */
const servedName = computed(() =>
  form.value.served_model_name.trim() ||
  form.value.model_path.trim().replace(/\/+$/, '').split('/').pop() || '')

const selectedSpace = computed(
  () => searchSpaces.value.find((s) => s.id === selectedSpaceId.value) ?? null)
const selectedObjective = computed(
  () => objectives.value.find((o) => o.id === selectedObjectiveId.value) ?? null)
const selectedVerifyObjective = computed(
  () => objectives.value.find((o) => o.id === verifyObjectiveId.value) ?? null)

/** On only when the second workload is complete — the API refuses half of it. */
const staged = computed(
  () => form.value.verify_enabled &&
    (verifyWorkload.value.mode !== 'existing' || verifyWorkload.value.slug.trim().length > 0))

/** The objectives a module's results can be ranked by. */
function objectivesFor(module: string): Objective[] {
  if (!module) return objectives.value
  return objectives.value.filter((o) => o.target_metric.startsWith(`${module}.`))
}
const screenObjectives = computed(() => objectivesFor(screenModule.value))
const verifyObjectives = computed(() => objectivesFor(verifyModule.value))

/** A different workload reports different metric names: move the objective
 *  to one it can report, preferring a built-in, unless the current one fits. */
watch(screenModule, () => {
  const fits = screenObjectives.value
  if (!fits.length || fits.some((o) => o.id === selectedObjectiveId.value)) return
  selectedObjectiveId.value = (fits.find((o) => o.is_builtin) ?? fits[0]).id
})
watch(verifyModule, () => {
  if (verifyObjectives.value.some((o) => o.id === verifyObjectiveId.value)) return
  verifyObjectiveId.value = null
})

/** The rolling dataset this campaign replays, so it is pinned for its life:
 *  a described replay names it; an existing benchmark may resolve one. */
function replaysOf(w: Workload): string {
  if (w.mode === 'replay') return w.dataset_profile
  if (w.mode === 'existing') return benchmarks.value.find((b) => b.slug === w.slug)?.replays_profile ?? ''
  return ''
}
const pinnedProfile = computed(() =>
  (staged.value ? replaysOf(verifyWorkload.value) : '') || replaysOf(workload.value) ||
  form.value.dataset_profile)

/** What the summary calls a workload. */
function describeWorkload(w: Workload): string {
  if (w.mode === 'sweep') {
    return `synthetic ${w.input_tokens} in / ${w.output_tokens} out at ${w.concurrencies}`
  }
  if (w.mode === 'replay') return `replay of ${w.dataset_profile || 'the example set'}`
  return w.slug.trim() || 'the platform default'
}

/** The policy as a checkbox: two words beat a dropdown reading
 *  "rebuild_at_start / use_current". */
const rebuildAtStart = computed({
  get: () => form.value.dataset_policy !== 'use_current',
  set: (on: boolean) => {
    form.value.dataset_policy = on ? 'rebuild_at_start' : 'use_current'
  },
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

/** An objective naming metrics its workload does not report scores every run
 *  None — which reads, in the morning, as "nothing was measured". */
const objectiveMismatch = computed(() => {
  const target = selectedObjective.value?.target_metric ?? ''
  return Boolean(screenModule.value && target && !target.startsWith(`${screenModule.value}.`))
})
const verifyObjectiveMismatch = computed(() => {
  const target = selectedVerifyObjective.value?.target_metric ?? ''
  return Boolean(verifyModule.value && target && !target.startsWith(`${verifyModule.value}.`))
})

/** What each step still needs, in the words of the thing that is missing.
 *  Shown on the step itself rather than only on submit, so nobody reaches the
 *  end and is told the first page was wrong. */
const problems = computed<string[][]>(() => [
  [
    !form.value.image.trim() && 'a container image',
    !form.value.model_path.trim() && 'the model path on the machine',
  ].filter(Boolean) as string[],
  [] as string[], // machines may be left empty (= any), so nothing is required
  [!selectedSpace.value && 'a search space'].filter(Boolean) as string[],
  [
    !selectedObjective.value && 'an objective',
    // Ticked but blank is the quiet failure: the campaign saves, the second
    // stage never fires, and nothing anywhere says why.
    form.value.verify_enabled && !staged.value && 'the benchmark to re-measure the best on',
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
/** The benchmark half of a campaign body: a described workload goes as a
 *  spec (the API creates its benchmark), an existing one as its slug. */
function benchmarkFields() {
  const verify = staged.value ? verifyWorkload.value : null
  return {
    benchmark_spec: specOf(workload.value),
    benchmark_slug: workload.value.mode === 'existing' ? workload.value.slug.trim() : '',
    verify_benchmark_spec: verify ? specOf(verify) : null,
    verify_benchmark_slug: verify?.mode === 'existing' ? verify.slug.trim() : '',
    // A replay's dataset is pinned for the campaign's whole life.
    dataset_profile: pinnedProfile.value,
  }
}

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
      served_model_name: servedName.value,
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
      ...benchmarkFields(),
      objective: selectedObjective.value
        ? { target_metric: selectedObjective.value.target_metric }
        : {},
      verify_objective: verifyObjectivePayload.value,
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

/** Installed plugins that offer search strategies of their own. */
const strategyPlugins = plugins.filter((p) => p.strategies)
/** How the chosen plugin strategy reads, or null when none is chosen. */
const pluginStrategyText = computed(() =>
  describeStrategy(applyStrategy(form.value.extensions, form.value.strategy)))

/** The policy picked in the Strategy select, or null to enumerate the space. */
const selectedPolicy = computed<Policy | null>(() => {
  const m = /^policy:(\d+)$/.exec(form.value.strategy)
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
  try {
    benchmarks.value = (await api.get('/campaigns/benchmarks')).data
  } catch {
    benchmarks.value = []
  }
}

/** Preselect an objective rather than leaving it blank: a campaign created
 *  without one ranks on a fallback nobody chose. Prefer the platform default
 *  (what the default screen benchmark measures), then the first saved one. */
async function selectDefaultObjective() {
  let preferred = ''
  try {
    preferred = (await api.get('/objectives/metrics')).data.default_target_metric
  } catch {
    /* fall through to the platform default, then the first objective */
  }
  const pick =
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
  policies.value = (await api.get('/policies')).data
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
  // A policy campaign travels as policy_id + policy_settings; the form holds
  // them as the Strategy select and the three budget inputs.
  const policyId = parsed.fields.policy_id
  form.value.strategy = typeof policyId === 'number'
    ? `policy:${policyId}`
    : strategyFromExtensions(form.value.extensions)
  const { max_contenders, ...pinned } =
    (parsed.fields.policy_settings ?? {}) as Record<string, number>
  if (max_contenders) form.value.policy_max_contenders = max_contenders
  form.value.policy_pinned = pinned
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
  // An imported campaign names its benchmarks, so it keeps exactly those.
  workload.value = { ...defaultWorkload('existing'), slug: String(form.value.benchmark_slug ?? '') }
  const verifySlug = String(form.value.verify_benchmark_slug ?? '').trim()
  verifyWorkload.value = verifySlug
    ? { ...defaultWorkload('existing'), slug: verifySlug }
    : defaultWorkload('replay')
  form.value.verify_enabled = Boolean(verifySlug)
  advancedNames.value = form.value.verify_enabled ? ['split'] : []
  if (form.value.served_model_name || Object.keys(parsed.fields.extra_env ?? {}).length ||
    Object.keys(parsed.fields.extra_volumes ?? {}).length) modelAdvanced.value = ['model']

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

// -- draft -------------------------------------------------------------------

/** Where each drafted value came from, shown on the Check step until the
 *  author starts over. A derived value reviewed as if someone chose it is how a
 *  wrong image runs all night. */
const draftNotes = ref<{ provenance: Record<string, string>; warnings: string[] } | null>(null)
const draftOpen = ref(false)
const drafting = ref(false)
const baselines = ref<Baseline[]>([])
const draftForm = ref({
  baseline_id: null as number | null,
  served_model_name: '',
  node_group: '',
  verify_benchmark_slug: '',
})

async function openDraft() {
  draftOpen.value = true
  try {
    baselines.value = (await api.get('/baselines')).data
  } catch {
    baselines.value = []
  }
}

/** Ask the server for a campaign derived from a baseline, the fleet and
 *  LLMBench, then take it in exactly as an imported file — same fields, same
 *  jump to the checks. */
async function draftFrom(req: Record<string, unknown>) {
  drafting.value = true
  try {
    const { data } = await api.post('/campaigns/draft', req)
    const campaign = { ...data.campaign }
    const model = campaign.served_model_name || 'model'
    campaign.search_space = { name: `drafted: ${model}`, ...campaign.search_space }
    campaign.objective = { name: `drafted: ${campaign.objective.target_metric}`,
      ...campaign.objective }
    if (campaign.verify_objective?.target_metric) {
      campaign.verify_objective = {
        name: `drafted: ${campaign.verify_objective.target_metric}`,
        ...campaign.verify_objective,
      }
    }
    importText.value = toYaml(campaign)
    draftOpen.value = false
    applyImport()
    draftNotes.value = { provenance: data.provenance, warnings: data.warnings }
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Could not draft a campaign')
  } finally {
    drafting.value = false
  }
}

function submitDraft() {
  const f = draftForm.value
  draftFrom({
    baseline_id: f.baseline_id,
    served_model_name: f.baseline_id ? '' : f.served_model_name.trim(),
    node_group: f.node_group,
    verify_benchmark_slug: f.verify_benchmark_slug.trim(),
  })
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
      name: form.value.name.trim() || defaultName.value,
      engine: form.value.engine,
      image: form.value.image.trim(),
      model_path: form.value.model_path,
      served_model_name: servedName.value,
      extra_env: extras.env ?? {},
      extra_volumes: extras.volumes ?? {},
      machine_names: form.value.machine_names,
      node_group: form.value.node_group,
      service_port: form.value.service_port,
      share_machine: form.value.share_machine,
      max_run_minutes: form.value.max_run_minutes,
      ...benchmarkFields(),
      policy_id: selectedPolicy.value?.id ?? null,
      extensions: applyStrategy(form.value.extensions, form.value.strategy),
      policy_settings: selectedPolicy.value
        ? { ...form.value.policy_pinned, max_contenders: form.value.policy_max_contenders }
        : {},
      confirm_top_k: form.value.confirm_top_k,
      confirm_repeats: form.value.confirm_repeats,
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
      // Both halves, or neither: the API refuses a second benchmark with no
      // top-k and a top-k with none, because either one silently never fires.
      verify_top_k: staged.value ? form.value.verify_top_k : 0,
      verify_max_run_minutes: form.value.verify_max_run_minutes,
      verify_objective: verifyObjectivePayload.value,
      dataset_policy: form.value.dataset_policy,
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
  // "Tune from this" on the Baselines page opens this page with ?baseline=<id>.
  const fromBaseline = Number(route.query.baseline)
  if (fromBaseline) {
    router.replace({ query: {} })
    await draftFrom({ baseline_id: fromBaseline })
    return
  }
})
</script>

<template>
  <div class="page wide">
    <div class="header-row">
      <h1 class="page-title">New campaign</h1>
      <div>
        <el-button type="primary" plain @click="openDraft">Start from a baseline</el-button>
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
              <el-input v-model="form.name" :placeholder="defaultName" />
            </el-form-item>
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
            <el-collapse v-model="modelAdvanced" class="advanced">
              <el-collapse-item name="model" title="Advanced">
                <el-form-item>
                  <template #label>
                    <span>Served model name</span>
                    <InfoHint>
                      The name the engine answers to. Only the platform and LLMBench use it,
                      so by default it is the last part of the model path.
                    </InfoHint>
                  </template>
                  <el-input v-model="form.served_model_name"
                    :placeholder="servedName || 'from the model path'" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span>Launch extras</span>
                    <InfoHint>
                      YAML. Env vars and bind mounts every container gets — what the model
                      needs beyond the engine flags.
                    </InfoHint>
                  </template>
                  <el-input v-model="form.extras_text" type="textarea" :rows="4" class="mono" />
                </el-form-item>
              </el-collapse-item>
            </el-collapse>
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
            <el-form-item>
              <el-checkbox v-model="form.share_machine">Run candidates in parallel</el-checkbox>
              <InfoHint :width="340">
                Several candidates share a machine at once, each pinned to its own GPUs:
                an 8-card node running one tp=2 config would otherwise leave six cards
                idle. Widest candidates are placed first. Turn off if a config is
                sensitive to host-level contention.
              </InfoHint>
            </el-form-item>
            <el-collapse v-model="machinesAdvanced" class="advanced">
              <el-collapse-item name="machines" title="Advanced">
                <div class="grid-2">
                  <el-form-item>
                    <template #label>
                      <span>First engine port</span>
                      <InfoHint>
                        Each run takes the first free port from here. 30000–32767 is
                        skipped: on k8s nodes kube-proxy hijacks that range.
                      </InfoHint>
                    </template>
                    <el-input-number v-model="form.service_port" :min="1024" :max="65535" />
                  </el-form-item>
                  <el-form-item>
                    <template #label>
                      <span>Max minutes per run</span>
                      <InfoHint>
                        A safety bound. The window is planned from how long this campaign's
                        runs actually take, never more than this.
                      </InfoHint>
                    </template>
                    <el-input-number v-model="form.max_run_minutes" :min="10" :max="1440"
                      :step="60" />
                  </el-form-item>
                </div>
              </el-collapse-item>
            </el-collapse>
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
                  With <b>no policy</b> the campaign tries every configuration in the
                  space, in declaration order — exhaustive and repeatable, which works
                  while the space is small; a window that runs out of time stops wherever
                  it stopped. A <b>policy</b> is an external container (policy-as-code)
                  that decides what to try next; the platform still launches, benchmarks
                  and judges every configuration it picks.
                </InfoHint>
                <el-button link type="primary" class="label-link"
                  @click="openBuilder('/policies')">Register one</el-button>
                <el-button link class="label-link" @click="refreshLists">Refresh</el-button>
              </template>
              <el-select v-model="form.strategy" style="width: 100%" filterable>
                <el-option value="" label="No policy — every configuration, in order" />
                <el-option-group v-if="policies.length" label="Policy containers">
                  <el-option v-for="p in policies" :key="p.id" :value="`policy:${p.id}`"
                    :label="`Policy — ${p.name}`">
                    <span>{{ p.name }}</span>
                    <span class="muted opt-help mono">{{ p.image }}</span>
                  </el-option>
                </el-option-group>
                <el-option-group v-for="p in strategyPlugins" :key="p.name"
                  :label="p.strategies!.group">
                  <el-option v-for="o in p.strategies!.options" :key="o.value"
                    :value="strategyValue(p.name, o.value)" :label="o.label">
                    <span>{{ o.label }}</span>
                    <span v-if="o.help" class="muted opt-help">{{ o.help }}</span>
                  </el-option>
                </el-option-group>
              </el-select>
            </el-form-item>

            <el-form-item v-if="selectedPolicy" class="spaced">
              <template #label>
                <span>Finalists to re-measure</span>
                <InfoHint :width="360">
                  When the window nears its end the policy names its best configs, and the
                  platform launches and benchmarks each one itself — that measurement
                  decides the winner. The time this needs is held back from the search,
                  learned from how long this campaign's runs take.
                </InfoHint>
              </template>
              <el-input-number v-model="form.policy_max_contenders" :min="1" :max="8"
                size="small" />
            </el-form-item>

            <el-collapse v-if="!selectedPolicy" v-model="searchAdvanced" class="advanced">
              <el-collapse-item name="search" title="Advanced">
                <el-form-item>
                  <template #label>
                    <span>Re-run the best</span>
                    <InfoHint :width="360">
                      One benchmark is a signal, not a decision: a config can lead by noise.
                      Before finishing, re-run the best few configs so the winner is backed
                      by several measurements and reports its spread.
                    </InfoHint>
                  </template>
                  <div class="sentence">
                    <el-input-number v-model="form.confirm_top_k" :min="0" :max="10"
                      size="small" controls-position="right" class="inline-num" />
                    <span>configs,</span>
                    <el-input-number v-model="form.confirm_repeats" :min="2" :max="10"
                      size="small" controls-position="right" class="inline-num"
                      :disabled="form.confirm_top_k === 0" />
                    <span>times each</span>
                    <span class="muted tiny">{{ form.confirm_top_k === 0 ? '(off)' : '' }}</span>
                  </div>
                </el-form-item>
              </el-collapse-item>
            </el-collapse>
          </el-form>
        </template>

        <!-- 4. Goal — what every candidate is measured with, and what wins.
             The workload is described, not looked up: AutoTune creates the
             matching benchmark on LLMBench with the campaign. Re-measuring the
             best few on a second workload is an opt-in under Advanced. -->
        <template v-if="step === 3">
          <el-form label-position="top">
            <el-form-item>
              <template #label>
                <span>How each candidate is measured</span>
                <InfoHint :width="380">
                  Every candidate gets the same load from LLMBench, the benchmark platform.
                  Describe the load and AutoTune creates a matching benchmark there, filed
                  under <span class="mono">llm-autotune</span> and locked so it cannot change
                  mid-campaign. The same load always maps to the same benchmark, so
                  campaigns that share it can be compared.
                </InfoHint>
              </template>
              <WorkloadPicker v-model="workload" v-model:rebuild="rebuildAtStart"
                :profiles="datasetProfiles" :benchmarks="benchmarks"
                @module="(m) => (screenModule = m)" />
            </el-form-item>

            <el-form-item>
              <template #label>
                <span>What wins</span>
                <InfoHint :width="340">
                  The metric candidates are ranked on, and the redlines a candidate must
                  hold to rank at all. Only objectives this workload can report are listed.
                </InfoHint>
                <el-button link type="primary" class="label-link"
                  @click="openBuilder('/objectives')">Build one</el-button>
                <el-button link class="label-link" @click="refreshLists">Refresh</el-button>
              </template>
              <el-select v-model="selectedObjectiveId" filterable style="width: 100%"
                placeholder="pick an objective">
                <el-option v-for="o in screenObjectives" :key="o.id" :value="o.id" :label="o.name">
                  <span>{{ o.name }}</span>
                  <span class="muted opt-help">{{ o.direction }} {{ o.target_metric }}</span>
                </el-option>
              </el-select>
              <div v-if="selectedObjective" class="goal-box">
                <div class="goal-line">
                  <span class="muted tiny lead-in">rank by</span>
                  <b>{{ selectedObjective.direction }}</b>
                  <span class="mono">{{ selectedObjective.target_metric }}</span>
                </div>
                <!-- "must hold", not "reject if": a redline is the condition a run
                     has to SATISFY. -->
                <div v-if="selectedObjective.redlines.length" class="goal-line">
                  <span class="muted tiny lead-in">must hold</span>
                  <span v-for="(r, i) in selectedObjective.redlines" :key="i" class="mono chip">
                    {{ r.metric }} {{ r.op }} {{ r.value }}
                  </span>
                </div>
                <div v-else class="muted tiny">no redlines — every successful run qualifies</div>
              </div>
              <div v-if="objectiveMismatch" class="stage-warn">
                <b>{{ selectedObjective?.target_metric }}</b> is not something this workload
                reports, so every run would score nothing. Pick one from the list.
              </div>
            </el-form-item>

            <el-collapse v-model="advancedNames" class="advanced">
              <el-collapse-item name="split">
                <template #title>Advanced: re-measure the best on a second workload</template>
                <div class="stage-box" :class="{ off: !form.verify_enabled }">
                  <div class="stage-head">
                    <el-checkbox v-model="form.verify_enabled">
                      <h4>Screen everything, then re-measure the best</h4>
                    </el-checkbox>
                    <InfoHint :width="380">
                      A replay of real traffic takes the better part of an hour per config.
                      Screen every candidate with the workload above, then replay only the
                      best few — and let that measurement decide the winner.
                    </InfoHint>
                  </div>
                  <template v-if="form.verify_enabled">
                    <WorkloadPicker v-model="verifyWorkload" v-model:rebuild="rebuildAtStart"
                      :profiles="datasetProfiles" :benchmarks="benchmarks" no-sweep
                      @module="(m) => (verifyModule = m)" />
                    <div class="sentence">
                      <span>Re-measure the best</span>
                      <el-input-number v-model="form.verify_top_k" :min="1" :max="10"
                        size="small" controls-position="right" class="inline-num" />
                      <span>, ranked by</span>
                      <el-select v-model="verifyObjectiveId" size="small" clearable filterable
                        class="verify-objective" placeholder="the platform default">
                        <el-option v-for="o in verifyObjectives" :key="o.id" :value="o.id"
                          :label="o.name">
                          <span>{{ o.name }}</span>
                          <span class="muted opt-help">{{ o.target_metric }}</span>
                        </el-option>
                      </el-select>
                    </div>
                    <div v-if="verifyObjectiveMismatch" class="stage-warn">
                      <b>{{ selectedVerifyObjective?.target_metric }}</b> is not something the
                      second workload reports, so every re-measured run would score nothing.
                    </div>
                  </template>
                  <div v-else class="muted tiny">Off — the workload above decides the winner.</div>
                </div>
              </el-collapse-item>
            </el-collapse>
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
              hand." />
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

          <div v-if="draftNotes" class="draft-notes">
            <el-alert v-for="w in draftNotes.warnings" :key="w" type="warning"
              :closable="false" show-icon :title="w" />
            <details>
              <summary class="muted tiny">
                Drafted — where {{ Object.keys(draftNotes.provenance).length }} values came from
              </summary>
              <dl class="provenance">
                <template v-for="(why, field) in draftNotes.provenance" :key="field">
                  <dt class="mono">{{ field }}</dt>
                  <dd>{{ why }}</dd>
                </template>
              </dl>
            </details>
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
          <dd>{{ form.name || defaultName }}</dd>
          <dt>Serving</dt>
          <dd :class="{ empty: !servedName }">
            {{ servedName || '—' }} <span class="muted">on {{ form.engine }}</span>
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
                · {{ selectedPolicy ? `policy ${selectedPolicy.name}`
                  : (pluginStrategyText ?? 'every configuration, in order') }}</span>
            </template>
            <template v-else>not chosen</template>
          </dd>
          <dt>Measured by</dt>
          <dd>{{ describeWorkload(workload) }}</dd>
          <dt>Wins</dt>
          <dd :class="{ empty: !selectedObjective }">
            {{ selectedObjective?.name ?? 'no objective chosen' }}
          </dd>
          <template v-if="staged">
            <dt>Then</dt>
            <dd>the best {{ form.verify_top_k }} on {{ describeWorkload(verifyWorkload) }}</dd>
          </template>
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

    <el-dialog v-model="draftOpen" title="Start from what the platform knows" width="620px">
      <p class="muted lead">
        The image, model and production arguments come from the baseline; the grid is
        sized to the machines; the dataset comes from LLMBench. Every value says where it
        came from, and nothing is created until you press Create.
      </p>
      <el-form label-position="top">
        <el-form-item label="Baseline">
          <el-select v-model="draftForm.baseline_id" clearable filterable
            placeholder="pick one — or name a model below">
            <el-option v-for="b in baselines" :key="b.id" :value="b.id"
              :label="`#${b.id} ${b.served_model_name} · ${b.engine} · ${b.card_type || 'any card'}`" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="!draftForm.baseline_id" label="Model (finds its baseline for these cards)">
          <el-input v-model="draftForm.served_model_name" placeholder="served model name" />
        </el-form-item>
        <el-form-item label="Node group (optional — otherwise every leased machine)">
          <el-select v-model="draftForm.node_group" clearable placeholder="single-node">
            <el-option v-for="g in groups" :key="g.name" :value="g.name" :label="g.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="Verify the best few on (optional)">
          <el-select v-model="draftForm.verify_benchmark_slug" clearable filterable allow-create
            default-first-option class="mono" placeholder="no second stage">
            <el-option v-for="b in benchmarks" :key="b.slug" :value="b.slug" :label="b.slug" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="draftOpen = false">Cancel</el-button>
        <el-button type="primary" :loading="drafting"
          :disabled="!draftForm.baseline_id && !draftForm.served_model_name.trim()"
          @click="submitDraft">
          Draft and check
        </el-button>
      </template>
    </el-dialog>

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
.verify-objective {
  width: 220px;
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
.draft-notes {
  display: grid;
  gap: 8px;
  margin-bottom: 12px;
}
.provenance {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 8px 0 0;
  font-size: 12px;
}
.provenance dd {
  margin: 0;
}
</style>
