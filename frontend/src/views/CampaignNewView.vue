<script setup lang="ts">
import { apiErrorText } from '../utils/apiError'
/** Building a campaign, five decisions at a time.
 *
 *  This was one dialog with nineteen controls in it, which asked the author to
 *  hold the whole thing in their head at once and gave no signal about which
 *  parts they had actually finished. The steps here are the questions a
 *  campaign really answers — what to serve, where, what to try, what counts as
 *  better, and when — and the panel on the right is the answer so far, visible
 *  the entire time rather than at a review screen nobody reads.
 */
import { ArrowDown, CopyDocument, Document, Medal } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch, type Ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  api,
  type Baseline,
  type Campaign,
  type DatasetProfile,
  type FrozenDataset,
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
import { type Extensions } from '../plugins'
import { campaignFromYaml, campaignToYaml } from '../utils/campaignYaml'
import { rememberValues, withPlatformValues } from '../utils/recent'
import { absoluteTime } from '../utils/time'
import { sweptKeysOf } from '../utils/space'
import { defaultWorkload, specOf, type BenchmarkChoice, type Workload } from '../utils/workload'
import { fromYaml, toYaml, YamlError } from '../utils/yaml'
import { all } from '../utils/load'
import { useI18n } from '../i18n'

const { t } = useI18n()
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
const frozenDatasets = ref<FrozenDataset[]>([])
/** What LLMBench will run a submission against, for "an existing benchmark".
 *  Empty when LLMBench is unreachable — the picker still takes a typed slug. */
const benchmarks = ref<BenchmarkChoice[]>([])
/** What every candidate is measured with, and (two-stage) what the best few
 *  are re-measured with. Described workloads become benchmarks on create. */
const workload = ref<Workload>(defaultWorkload('replay'))
const verifyWorkload = ref<Workload>(defaultWorkload('replay'))
/** The LLMBench module each workload runs — the prefix of its metric names,
 *  which is what decides the objectives that can rank it. '' = unknown. */
const screenModule = ref('')
const verifyModule = ref('')
/** Registered policy containers. Picking one in the Strategy select makes this
 *  a policy-as-code campaign: the container searches, the platform judges. */
const policies = ref<Policy[]>([])
/** The parameters a policy may not change when a campaign keeps the default. */
const forbiddenDefault = ref<string[]>([])
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
const policyAdvanced = ref<string[]>([])

const form = ref({
  name: '',
  // 'image': an image and the engine flags the search space sets.
  // 'recipe': the engine chart's values (a model-catalog variant, an
  // LLMService, or a command line read into one); the space addresses it.
  config_mode: 'image' as 'image' | 'recipe',
  recipe_text: '',
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
  strategy: '',
  // What installed plugins keep about the campaign, by plugin name; carried
  // through from an imported or copied campaign.
  extensions: {} as Extensions,
  // Only sent for a policy campaign: how many of its finalists the platform
  // re-measures at the end. Their benchmark and startup time are learned.
  policy_max_contenders: 2,
  // The night's GPU budget (policy_settings.cards); undefined = the default
  // (the whole box, or one launch's worth on a node pool).
  policy_gpu_budget: undefined as number | undefined,
  policy_validate_production: true,
  // Timings an imported campaign pinned, carried through untouched.
  policy_pinned: {} as Record<string, number>,
  // Parameters the policy may not change. Off = the platform's default list
  // (flags that change answers rather than speed); on = this list, one glob
  // per line, where an empty list forbids nothing.
  policy_forbidden_custom: false,
  policy_forbidden_text: '',
  // Knobs for the policy itself, handed to it verbatim (YAML).
  policy_params_text: '',
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
  dataset_frozen: '',
  dataset_policy: 'rebuild_at_start',
  daily_start: '23:00',
  daily_end: '08:00',
  schedule_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  schedule_until: null as string | null,
})

/** What the recipe text reads as, from the API (`POST /campaigns/recipe`). */
interface RecipeRead {
  source: string
  recipe: Record<string, unknown>
  yaml: string
  engine: string
  image: string
  served_model_name: string
  notes: string[]
  problems: { kind: string; param: string; message: string }[]
  warnings: string[]
  shape: { topology: string; total_gpus: number; pods: { role: string; gpus: number; count: number }[] }
}
const recipeRead = ref<RecipeRead | null>(null)
const recipeError = ref('')
const readingRecipe = ref(false)
const recipeMode = computed(() => form.value.config_mode === 'recipe')

async function readRecipe() {
  recipeError.value = ''
  recipeRead.value = null
  if (!form.value.recipe_text.trim()) return
  readingRecipe.value = true
  try {
    const { data } = await api.post<RecipeRead>('/campaigns/recipe', { text: form.value.recipe_text })
    recipeRead.value = data
    form.value.engine = data.engine
  } catch (error: any) {
    recipeError.value = apiErrorText(error, t('campaignNew.recipe.unreadable'))
  } finally {
    readingRecipe.value = false
  }
}

const STEPS = computed(() => [
  { title: t('campaignNew.steps.model') },
  { title: t('campaignNew.steps.machines') },
  { title: t('campaignNew.steps.search') },
  { title: t('campaignNew.steps.benchmark') },
  { title: t('campaignNew.steps.schedule') },
  { title: t('campaignNew.steps.check') },
])

/** The name a campaign gets when its author leaves it blank. */
/** Today's date where the person is, not in UTC: a campaign named in the
 *  evening must not carry tomorrow's or yesterday's date. */
function localDate(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
const defaultName = computed(() => `${servedName.value || 'model'} · ${localDate()}`)

/** What the engine serves the model as: the typed name, else the last part of
 *  the model path — the same rule the API applies to an empty field. */
const servedName = computed(() =>
  (recipeMode.value && recipeRead.value?.served_model_name) ||
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

/** Whether a module's results can report a target metric: its own
 *  `module.metric` names, or a measure (a name with no module, such as
 *  total_tpm_per_gpu), which the platform derives for every module. */
function reports(module: string, target: string): boolean {
  return !module || !target.includes('.') || target.startsWith(`${module}.`)
}

/** The objectives a module's results can be ranked by. */
function objectivesFor(module: string): Objective[] {
  return objectives.value.filter((o) => reports(module, o.target_metric))
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
  if (w.mode === 'replay') return w.dataset_frozen ? '' : w.dataset_profile
  if (w.mode === 'existing') return benchmarks.value.find((b) => b.slug === w.slug)?.replays_profile ?? ''
  return ''
}
const pinnedProfile = computed(() =>
  (staged.value ? replaysOf(verifyWorkload.value) : '') || replaysOf(workload.value) ||
  form.value.dataset_profile)

/** What the summary calls a workload. */
function describeWorkload(w: Workload): string {
  if (w.mode === 'sweep') {
    return t('campaignNew.describe.sweep', { in: w.input_tokens, out: w.output_tokens, c: w.concurrencies })
  }
  if (w.mode === 'replay') {
    return w.dataset_frozen ? t('campaignNew.describe.frozen', { name: w.dataset_frozen })
      : t('campaignNew.describe.replay', { name: w.dataset_profile || t('campaignNew.describe.example') })
  }
  return w.slug.trim() || t('campaignNew.describe.default')
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
  return Boolean(target && !reports(screenModule.value, target))
})
const verifyObjectiveMismatch = computed(() => {
  const target = selectedVerifyObjective.value?.target_metric ?? ''
  return Boolean(target && !reports(verifyModule.value, target))
})

/** What each step still needs, in the words of the thing that is missing.
 *  Shown on the step itself rather than only on submit, so nobody reaches the
 *  end and is told the first page was wrong. */
const problems = computed<string[][]>(() => [
  [
    !recipeMode.value && !form.value.image.trim() && t('campaignNew.need.image'),
    recipeMode.value && !recipeRead.value && t('campaignNew.need.recipe'),
    recipeMode.value && !!recipeRead.value?.problems.length && t('campaignNew.need.recipeOk'),
    !form.value.model_path.trim() && t('campaignNew.need.modelPath'),
  ].filter(Boolean) as string[],
  [] as string[], // machines may be left empty (= any), so nothing is required
  [!selectedSpace.value && t('campaignNew.need.space')].filter(Boolean) as string[],
  [
    !selectedObjective.value && t('campaignNew.need.objective'),
    // Ticked but blank is the quiet failure: the campaign saves, the second
    // stage never fires, and nothing anywhere says why.
    form.value.verify_enabled && !staged.value && t('campaignNew.need.verify'),
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
/** A policy campaign's settings: the finalist count, anything an imported
 *  campaign pinned, and — only when set here — its own forbidden list and the
 *  policy's knobs. Throws YamlError on a malformed knobs block. */
function policySettings(): Record<string, unknown> {
  const settings: Record<string, unknown> = {
    ...form.value.policy_pinned,
    max_contenders: form.value.policy_max_contenders,
  }
  if (form.value.policy_gpu_budget) settings.cards = form.value.policy_gpu_budget
  if (!form.value.policy_validate_production) settings.validate_production = false
  if (form.value.policy_forbidden_custom) {
    settings.forbidden_params = form.value.policy_forbidden_text
      .split('\n').map((line) => line.trim()).filter(Boolean)
  }
  const params = fromYaml(form.value.policy_params_text, 'Policy parameters')
  if (Object.keys(params).length) settings.params = params
  return settings
}

function useCustomForbidden(on: string | number | boolean) {
  // Start a custom list from the default, so turning it on edits rather than
  // wipes it.
  if (on && !form.value.policy_forbidden_text.trim()) {
    form.value.policy_forbidden_text = forbiddenDefault.value.join('\n')
  }
}

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
      extra_volumes: recipeMode.value ? {} : (extras.volumes ?? {}),
      recipe: recipeMode.value ? (recipeRead.value?.recipe ?? {}) : {},
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
      policy_id: selectedPolicy.value?.id ?? null,
    })
    preflight.value = data
  } catch (error: any) {
    ElMessage.error(
      error instanceof YamlError ? error.message
        : (apiErrorText(error, t('campaignNew.err.checks'))),
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
  if (i === STEPS.value.length - 1 && checkable.value && !preflight.value) runPreflight()
})

const canCreate = computed(() => problems.value.every((p) => p.length === 0))

/** What the machine-side probes need before they can say anything: an image to
 *  look for, a model path to stat, and a space to size the widest candidate
 *  from. Name and objective are not among them. */
const missingForCheck = computed(() =>
  [
    !form.value.image.trim() && t('campaignNew.need.image2'),
    !form.value.model_path.trim() && t('campaignNew.need.modelPath2'),
    !selectedSpace.value && t('campaignNew.need.space'),
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
  // The last step is the checks: it is done when they ran, not when it was
  // merely looked at, and it is a problem when they found one.
  if (i === STEPS.value.length - 1) {
    if (!preflight.value || preflight.value.note) return 'wait'
    return blockers.value.length ? 'error' : 'success'
  }
  if (!visited.value.has(i)) return 'wait'
  return problems.value[i].length ? 'error' : 'success'
}

/** The policy picked in the Strategy select, or null to enumerate the space. */
const selectedPolicy = computed<Policy | null>(() => {
  const m = /^policy:(\d+)$/.exec(form.value.strategy)
  return m ? policies.value.find((p) => p.id === Number(m[1])) ?? null : null
})

async function load() {
  // At once: on a slow link, one after another meant ten round trips before
  // the form's lists were filled. The extras just stay empty if they fail.
  const [ms, gs, ps, spaces, objs] = await all(
    api.get('/machines'),
    api.get('/machine-groups').catch(() => null),
    api.get('/policies').catch(() => null),
    api.get('/search-spaces'),
    api.get('/objectives'),
    // These two leave this platform.
    loadDatasetProfiles(),
    api.get('/campaigns/benchmarks')
      .then(({ data }) => (benchmarks.value = data))
      .catch(() => (benchmarks.value = [])),
    api.get('/policies/defaults')
      .then(({ data }) => (forbiddenDefault.value = data.forbidden_params ?? []))
      .catch(() => (forbiddenDefault.value = [])),
  )
  machines.value = ms.data
  groups.value = gs?.data ?? []
  policies.value = ps?.data ?? []
  searchSpaces.value = spaces.data
  objectives.value = objs.data
  if (selectedObjectiveId.value === null) await selectDefaultObjective()
}

/** Preselect an objective rather than leaving it blank: a campaign created
 *  without one ranks on a fallback nobody chose. Prefer the platform default
 *  (what the default screen benchmark measures), then the first saved one. */
// Allowed to fail: it is the only list that leaves this platform, and a
// benchmark platform that is briefly unreachable must not stop anyone writing a
// campaign. The field falls back to free text.
async function loadDatasetProfiles() {
  await Promise.all([
    api.get('/campaigns/dataset-profiles')
      .then(({ data }) => (datasetProfiles.value = data))
      .catch(() => (datasetProfiles.value = [])),
    // An LLMBench too old to share them just offers none.
    api.get('/datasets/frozen')
      .then(({ data }) => (frozenDatasets.value = data.frozen))
      .catch(() => (frozenDatasets.value = [])),
  ])
}

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
  const [spaces, objs, ps] = await all(
    api.get('/search-spaces'), api.get('/objectives'), api.get('/policies'))
  searchSpaces.value = spaces.data
  objectives.value = objs.data
  policies.value = ps.data
  ElMessage.success(t('campaignNew.msg.refreshed'))
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
  return t('campaignNew.estimate', { h: hours.toFixed(1), n: nights })
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
  exact = false,
): number {
  let name = String(incoming.name ?? '')
  const saved = name ? list.value.find((row: T) => row.name === name) : undefined
  // A copy of a campaign keeps the space and objective it RAN with: a saved
  // row of the same name that was edited since is not that, so it is left be.
  const same = (row: T) => Object.entries(incoming).every(([key, value]) =>
    key === 'name' || JSON.stringify((row as Record<string, unknown>)[key] ?? null)
      === JSON.stringify(value ?? null))
  if (saved && (!exact || same(saved))) return saved.id
  if (saved) name = `${name} (as copied)`
  const row = { ...extra, ...incoming, id, name: name || '(imported)' } as unknown as T
  list.value = [row, ...list.value.filter((r: T) => r.id !== id)]
  return id
}

/** An imported or drafted space is not a saved row, so it has no count of
 *  its own: ask the platform, the way the search-space editor's preview does. */
async function countInlineSpace() {
  const row = searchSpaces.value.find((r) => r.id === selectedSpaceId.value)
  if (!row || row.id >= 0) return
  try {
    const { data } = await api.post('/search-spaces/preview', {
      name: row.name, engine: row.engine, base: row.base ?? {}, grid: row.grid ?? {},
      tied: row.tied ?? [], range: row.range ?? {}, conditions: row.conditions ?? {},
    })
    row.candidate_count = data.candidate_count
    searchSpaces.value = [...searchSpaces.value]
  } catch {
    // The checks report a space the platform cannot expand.
  }
}

/** `review`: fill the form in and leave it on the first step to be changed
 *  (a copy of a campaign), rather than going straight to the checks. */
function applyImport(opts: { review?: boolean } = {}) {
  let parsed
  try {
    parsed = campaignFromYaml(importText.value)
  } catch (error: any) {
    ElMessage.error(error?.message ?? t('campaignNew.err.yaml'))
    return
  }

  // Scalars and maps land straight on the form; anything absent keeps the
  // default already there rather than being blanked.
  for (const [key, value] of Object.entries(parsed.fields)) {
    if (key === 'extra_env' || key === 'extra_volumes') continue
    // A file written by hand says "no window" as null; the form (and the API)
    // spell it as an empty string.
    const typed = value === null && (key === 'daily_start' || key === 'daily_end') ? '' : value
    if (key in form.value) (form.value as Record<string, unknown>)[key] = typed
  }
  // A policy campaign travels as policy_id + policy_settings; the form holds
  // them as the Strategy select and the three budget inputs.
  const policyId = parsed.fields.policy_id
  form.value.strategy = typeof policyId === 'number' ? `policy:${policyId}` : ''
  const { max_contenders, forbidden_params, params, cards, validate_production, ...pinned } =
    (parsed.fields.policy_settings ?? {}) as Record<string, unknown>
  if (typeof max_contenders === 'number') form.value.policy_max_contenders = max_contenders
  form.value.policy_gpu_budget = typeof cards === 'number' ? cards : undefined
  form.value.policy_validate_production = validate_production !== false
  form.value.policy_pinned = pinned as Record<string, number>
  form.value.policy_forbidden_custom = Array.isArray(forbidden_params)
  form.value.policy_forbidden_text = Array.isArray(forbidden_params)
    ? forbidden_params.join('\n') : ''
  form.value.policy_params_text =
    params && typeof params === 'object' && Object.keys(params).length ? toYaml(params) : ''

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
    } as any, opts.review)
    void countInlineSpace()
  }
  if (parsed.objective) {
    selectedObjectiveId.value = adopt(objectives, parsed.objective, -2, {
      owner_id: null, description: 'from the imported file', redlines: [],
      direction: 'maximize', is_builtin: false, created_at: '', updated_at: '',
    } as any, opts.review)
  }
  if (parsed.verify_objective && Object.keys(parsed.verify_objective).length) {
    verifyObjectiveId.value = adopt(objectives, parsed.verify_objective, -3, {
      owner_id: null, description: 'from the imported file', redlines: [],
      direction: 'maximize', is_builtin: false, created_at: '', updated_at: '',
    } as any, opts.review)
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
    ElMessage.warning(t('campaignNew.msg.unknown', { fields: parsed.unknown.join(', ') }))
  }

  visited.value = new Set([0, 1, 2, 3, 4, 5])
  if (opts.review) {
    step.value = 0
    return
  }
  // Straight to the checks: an imported campaign is one nobody has reviewed
  // against tonight's machines, and that is the whole point of this step.
  step.value = STEPS.value.length - 1
  ElMessage.success(t('campaignNew.msg.imported'))
  runPreflight()
}

// -- start from a previous campaign --------------------------------------------

/** Every campaign on the platform, newest first: the ones to start from, and
 *  where the field suggestions come from. */
const pastCampaigns = ref<Campaign[]>([])
const fromOpen = ref(false)
const fromId = ref<number | null>(null)
/** The campaign this form was filled in from, until the author starts over. */
const copiedFrom = ref<{ id: number; name: string } | null>(null)

async function loadPastCampaigns() {
  try {
    pastCampaigns.value = (await api.get('/campaigns')).data
  } catch {
    pastCampaigns.value = []
  }
}

/** "sweep" → "sweep v2" → "sweep v3", skipping names already taken. */
function nextName(name: string): string {
  const m = name.match(/^(.*?)\s+v(\d+)$/)
  const base = m ? m[1] : name
  let n = m ? Number(m[2]) + 1 : 2
  const taken = new Set(pastCampaigns.value.map((c) => c.name))
  while (taken.has(`${base} v${n}`)) n += 1
  return `${base} v${n}`
}

/** Fill the form in from a campaign's definition (never its results or its
 *  window) and leave it on the first step, to change before creating. */
async function startFromCampaign(id: number) {
  try {
    const campaign = (await api.get(`/campaigns/${id}`)).data as Campaign
    importText.value = campaignToYaml(
      { ...campaign, name: nextName(campaign.name) } as unknown as Record<string, unknown>)
    fromOpen.value = false
    applyImport({ review: true })
    draftNotes.value = null
    copiedFrom.value = { id: campaign.id, name: campaign.name }
    ElMessage.success(t('campaignNew.msg.filled', { name: campaign.name }))
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('campaignNew.err.load')))
  }
}

function startFrom(command: string) {
  if (command === 'campaign') {
    fromId.value = pastCampaigns.value[0]?.id ?? null
    fromOpen.value = true
  } else if (command === 'baseline') void openDraft()
  else if (command === 'yaml') importOpen.value = true
}

/** What was typed here before and what the platform's campaigns used, as
 *  suggestions for a text field. */
function suggest(field: 'image' | 'model_path' | 'served_model_name') {
  return (query: string, done: (items: { value: string }[]) => void) => {
    const q = query.trim().toLowerCase()
    done(withPlatformValues(field, pastCampaigns.value.map((c) => c[field]))
      .filter((v) => !q || v.toLowerCase().includes(q))
      .map((value) => ({ value })))
  }
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

/** A drafted objective's name: the saved objective it is identical to (the
 *  built-in one, usually), so the import selects that row; else its metric. */
function objectiveName(objective: Partial<Objective>): string {
  const key = (o: Partial<Objective>) => JSON.stringify([
    o.target_metric, o.direction ?? 'maximize',
    [...(o.redlines ?? [])].map((r) => [r.metric, r.op, Number(r.value)]).sort(),
  ])
  const same = objectives.value
    .filter((o) => o.id > 0 && key(o) === key(objective))
    .sort((a, b) => Number(b.is_builtin) - Number(a.is_builtin))
  return same[0]?.name ?? `${objective.target_metric} (drafted)`
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
    campaign.search_space = { name: `${model} (drafted)`, ...campaign.search_space }
    campaign.objective = { name: objectiveName(campaign.objective), ...campaign.objective }
    if (campaign.verify_objective?.target_metric) {
      campaign.verify_objective = {
        name: objectiveName(campaign.verify_objective), ...campaign.verify_objective,
      }
    }
    importText.value = toYaml(campaign)
    draftOpen.value = false
    applyImport()
    copiedFrom.value = null
    draftNotes.value = { provenance: data.provenance, warnings: data.warnings }
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('campaignNew.err.draft')))
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
    ElMessage.error(t('campaignNew.msg.stillNeeds', { items: problems.value[firstBad].join(', ') }))
    return
  }
  if (blockers.value.length) {
    try {
      await ElMessageBox.confirm(
        t('campaignNew.withProblems', { n: blockers.value.length, list: blockers.value.map((b) => b.label).join(', ') }),
        t('campaignNew.withProblemsTitle'),
        { confirmButtonText: t('campaignNew.createAnyway'), cancelButtonText: t('campaignNew.goBack'), type: 'warning' },
      )
    } catch {
      return
    }
  }
  busy.value = true
  try {
    const extras = fromYaml<{ env?: Record<string, string>; volumes?: Record<string, string> }>(
      form.value.extras_text, 'Launch extras')
    const { data } = await api.post('/campaigns', {
      name: form.value.name.trim() || defaultName.value,
      engine: form.value.engine,
      image: recipeMode.value ? '' : form.value.image.trim(),
      model_path: form.value.model_path,
      served_model_name: servedName.value,
      // With a recipe, env and volumes are the recipe's.
      extra_env: recipeMode.value ? {} : (extras.env ?? {}),
      extra_volumes: recipeMode.value ? {} : (extras.volumes ?? {}),
      recipe_text: recipeMode.value ? form.value.recipe_text : null,
      machine_names: form.value.machine_names,
      node_group: form.value.node_group,
      service_port: form.value.service_port,
      share_machine: form.value.share_machine,
      max_run_minutes: form.value.max_run_minutes,
      ...benchmarkFields(),
      policy_id: selectedPolicy.value?.id ?? null,
      extensions: form.value.extensions,
      policy_settings: selectedPolicy.value ? policySettings() : {},
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
    rememberValues({
      image: form.value.image,
      model_path: form.value.model_path,
      served_model_name: form.value.served_model_name,
    })
    router.push(`/campaigns/${data.id}`)
  } catch (error: any) {
    ElMessage.error(
      error instanceof YamlError
        ? error.message
        : (apiErrorText(error, t('campaignNew.err.create'))),
    )
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await Promise.all([load(), loadPastCampaigns()])
  // "Copy to a new campaign" on a campaign's page opens this page with ?from=<id>.
  const from = Number(route.query.from)
  if (from) {
    router.replace({ query: {} })
    await startFromCampaign(from)
    return
  }
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
      <h1 class="page-title">{{ t('campaignNew.title') }}</h1>
      <div class="header-actions">
        <el-dropdown trigger="click" @command="startFrom">
          <el-button type="primary" plain>
            {{ t('campaignNew.startFrom') }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="campaign" :icon="CopyDocument">
                {{ t('campaignNew.fromCampaign') }}</el-dropdown-item>
              <el-dropdown-item command="baseline" :icon="Medal">{{ t('campaignNew.fromBaseline') }}</el-dropdown-item>
              <el-dropdown-item command="yaml" :icon="Document">{{ t('campaignNew.fromYaml') }}</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button text @click="router.push('/campaigns')">{{ t('common.cancel') }}</el-button>
      </div>
    </div>
    <div v-if="copiedFrom" class="copied-from muted tiny">
      <el-icon><CopyDocument /></el-icon>
      {{ t('campaignNew.copiedFrom') }}
      <router-link :to="`/campaigns/${copiedFrom.id}`" class="nav-link" target="_blank">
        {{ copiedFrom.name }}</router-link>
    </div>

    <el-steps :active="step" finish-status="success" align-center class="steps">
      <el-step v-for="(s, i) in STEPS" :key="s.title" :title="s.title"
        :status="stepStatus(i)" class="clickable-step" @click="step = i" />
    </el-steps>

    <div class="split">
      <div class="panel">
        <!-- 1. Model -->
        <template v-if="step === 0">
          <el-form label-position="top">
            <el-form-item :label="t('campaignNew.name')">
              <el-input v-model="form.name" :placeholder="defaultName" />
            </el-form-item>
            <el-form-item>
              <template #label>
                <span>{{ t('campaignNew.recipe.mode') }}</span>
                <InfoHint :width="420">{{ t('campaignNew.recipe.modeHint') }}</InfoHint>
              </template>
              <el-radio-group v-model="form.config_mode">
                <el-radio-button value="image">{{ t('campaignNew.recipe.byImage') }}</el-radio-button>
                <el-radio-button value="recipe">{{ t('campaignNew.recipe.byRecipe') }}</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="!recipeMode" :label="t('campaignNew.image')">
              <el-autocomplete v-model="form.image" class="mono wide-input"
                :fetch-suggestions="suggest('image')"
                placeholder="lmsysorg/sglang:v0.5.13.post1" />
            </el-form-item>
            <template v-else>
              <el-form-item :label="t('campaignNew.recipe.text')">
                <el-input v-model="form.recipe_text" type="textarea" :rows="10" class="mono"
                  :placeholder="t('campaignNew.recipe.placeholder')" @change="readRecipe" />
                <div class="recipe-actions">
                  <el-button size="small" :loading="readingRecipe" @click="readRecipe">
                    {{ t('campaignNew.recipe.read') }}</el-button>
                  <span v-if="recipeError" class="danger small">{{ recipeError }}</span>
                </div>
              </el-form-item>
              <div v-if="recipeRead" class="recipe-summary small">
                <div>
                  <b>{{ recipeRead.engine }}</b> · <span class="mono">{{ recipeRead.image }}</span>
                  · {{ t('campaignNew.recipe.serves', { name: recipeRead.served_model_name || '—' }) }}
                </div>
                <div class="muted">
                  {{ t('campaignNew.recipe.shape', { topology: recipeRead.shape.topology, gpus: recipeRead.shape.total_gpus }) }}
                  <template v-for="g in recipeRead.shape.pods" :key="g.role">
                    · {{ g.role }} {{ g.count }}×{{ g.gpus }}</template>
                  · {{ t('campaignNew.recipe.source', { source: recipeRead.source }) }}
                </div>
                <el-alert v-for="p in recipeRead.problems" :key="p.param + p.message" type="error"
                  :closable="false" :title="p.message" />
                <div v-for="n in [...recipeRead.notes, ...recipeRead.warnings]" :key="n" class="muted">· {{ n }}</div>
              </div>
            </template>
            <el-form-item>
              <template #label>
                <span>{{ t('campaignNew.modelPath') }}</span>
                <InfoHint><span v-html="t('campaignNew.modelPathHint')" /></InfoHint>
              </template>
              <el-autocomplete v-model="form.model_path" class="mono wide-input"
                :fetch-suggestions="suggest('model_path')" placeholder="/data/models/qwen3.6" />
            </el-form-item>
            <el-collapse v-model="modelAdvanced" class="advanced">
              <el-collapse-item name="model" :title="t('campaignNew.advanced')">
                <el-form-item>
                  <template #label>
                    <span>{{ t('campaignNew.servedName') }}</span>
                    <InfoHint>{{ t('campaignNew.servedNameHint') }}</InfoHint>
                  </template>
                  <el-autocomplete v-model="form.served_model_name" class="wide-input"
                    :fetch-suggestions="suggest('served_model_name')"
                    :placeholder="servedName || t('campaignNew.fromPath')" />
                </el-form-item>
                <el-form-item v-if="!recipeMode">
                  <template #label>
                    <span>{{ t('campaignNew.extras') }}</span>
                    <InfoHint>{{ t('campaignNew.extrasHint') }}</InfoHint>
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
                <span>{{ t('campaignNew.nodeGroup') }}</span>
                <InfoHint :width="430"><span v-html="t('campaignNew.nodeGroupHint')" /></InfoHint>
              </template>
              <el-select v-model="form.node_group" clearable style="width: 100%"
                :placeholder="t('campaignNew.singleNode')">
                <el-option v-for="g in groups" :key="g.id" :value="g.name"
                  :label="`${g.name} — ${t('campaignNew.nNodes', { n: g.node_count })}${g.deployable ? '' : ' · ' + t('campaignNew.blocked')}`" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <template #label>
                <span>{{ t('campaignNew.machines') }}</span>
                <InfoHint>{{ t('campaignNew.machinesHint') }}</InfoHint>
              </template>
              <el-select v-model="form.machine_names" multiple style="width: 100%"
                :disabled="Boolean(form.node_group)"
                :placeholder="form.node_group ? t('campaignNew.groupIsPin') : t('campaignNew.anyMachine')">
                <el-option v-for="m in machines" :key="m.id" :value="m.name"
                  :label="`${m.name} (${m.gpu_count}× ${m.gpu_type || 'GPU'})`" />
              </el-select>
            </el-form-item>
            <el-form-item>
              <el-checkbox v-model="form.share_machine">{{ t('campaignNew.parallel') }}</el-checkbox>
              <InfoHint :width="340">
                {{ t('campaignNew.parallelHint') }}
              </InfoHint>
            </el-form-item>
            <el-collapse v-model="machinesAdvanced" class="advanced">
              <el-collapse-item name="machines" :title="t('campaignNew.advanced')">
                <div class="grid-2">
                  <el-form-item>
                    <template #label>
                      <span>{{ t('campaignNew.port') }}</span>
                      <InfoHint>{{ t('campaignNew.portHint') }}</InfoHint>
                    </template>
                    <el-input-number v-model="form.service_port" :min="1024" :max="65535" />
                  </el-form-item>
                  <el-form-item>
                    <template #label>
                      <span>{{ t('campaignNew.maxMinutes') }}</span>
                      <InfoHint>{{ t('campaignNew.maxMinutesHint') }}</InfoHint>
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
                <span>{{ t('campaignNew.space') }}</span>
                <el-button link type="primary" class="label-link"
                  @click="openBuilder('/search-spaces')">{{ t('campaignNew.build') }}</el-button>
                <el-button link class="label-link" @click="refreshLists">{{ t('common.refresh') }}</el-button>
              </template>
              <el-select v-model="selectedSpaceId" filterable style="width: 100%"
                :placeholder="t('campaignNew.pickSpace')" @change="applySearchSpace">
                <el-option v-for="s in searchSpaces" :key="s.id" :value="s.id"
                  :label="`${s.name} (${s.engine})`">
                  <span>{{ s.name }}</span>
                  <span class="muted opt-help">
                    {{ s.engine }} · {{ sweptKeysOf(s).join(', ') || t('campaignNew.noSweep') }}
                  </span>
                </el-option>
              </el-select>
            </el-form-item>

            <SpaceMap v-if="selectedSpace" :space="selectedSpace" compact
              :candidates="selectedSpace.candidate_count" />

            <el-form-item class="spaced">
              <template #label>
                <span>{{ t('campaignNew.strategy') }}</span>
                <InfoHint :width="360"><span v-html="t('campaignNew.strategyHint')" /></InfoHint>
                <el-button link type="primary" class="label-link"
                  @click="openBuilder('/policies')">{{ t('campaignNew.register') }}</el-button>
                <el-button link class="label-link" @click="refreshLists">{{ t('common.refresh') }}</el-button>
              </template>
              <el-select v-model="form.strategy" style="width: 100%" filterable
                :empty-values="[null, undefined]">
                <el-option value="" :label="t('campaignNew.grid')" />
                <el-option-group v-if="policies.length" label="Policies">
                  <el-option v-for="p in policies" :key="p.id" :value="`policy:${p.id}`"
                    :label="p.name">
                    <span>{{ p.name }}</span>
                    <span class="muted opt-help mono">{{ p.image }}</span>
                  </el-option>
                </el-option-group>
              </el-select>
            </el-form-item>

            <el-form-item v-if="selectedPolicy" class="spaced">
              <template #label>
                <span>{{ t('campaignNew.finalists') }}</span>
                <InfoHint :width="360">{{ t('campaignNew.finalistsHint') }}</InfoHint>
              </template>
              <el-input-number v-model="form.policy_max_contenders" :min="1" :max="8"
                size="small" />
            </el-form-item>
            <el-form-item>
              <template #label>
                <span>{{ t('campaignNew.gpuBudget') }}</span>
                <InfoHint :width="380">{{ t('campaignNew.gpuBudgetHint') }}</InfoHint>
              </template>
              <el-input-number v-model="form.policy_gpu_budget" :min="1" :max="64"
                size="small" :placeholder="t('campaignNew.default')" />
            </el-form-item>

            <el-collapse v-if="selectedPolicy" v-model="policyAdvanced" class="advanced">
              <el-collapse-item name="policy" :title="t('campaignNew.advanced')">
                <el-form-item>
                  <el-checkbox v-model="form.policy_validate_production">
                    {{ t('campaignNew.validateProd') }}
                  </el-checkbox>
                  <InfoHint :width="340">
                    {{ t('campaignNew.validateProdHint') }}
                  </InfoHint>
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span>{{ t('campaignNew.forbidden') }}</span>
                    <InfoHint :width="380">{{ t('campaignNew.forbiddenHint') }}</InfoHint>
                  </template>
                  <el-switch v-model="form.policy_forbidden_custom" :active-text="t('campaignNew.customList')"
                    :inactive-text="t('campaignNew.platformDefault')" @change="useCustomForbidden" />
                </el-form-item>
                <el-form-item v-if="form.policy_forbidden_custom">
                  <el-input v-model="form.policy_forbidden_text" type="textarea" :rows="6"
                    class="mono" :placeholder="t('campaignNew.emptyAnything')" />
                </el-form-item>
                <p v-else class="muted mono forbidden-default">
                  {{ forbiddenDefault.join(', ') || t('campaignNew.defaultList') }}
                </p>
                <el-form-item>
                  <template #label>
                    <span>{{ t('campaignNew.params') }}</span>
                    <InfoHint :width="360">{{ t('campaignNew.paramsHint') }}</InfoHint>
                  </template>
                  <el-input v-model="form.policy_params_text" type="textarea" :rows="4"
                    class="mono" placeholder="model: my-llm" />
                </el-form-item>
              </el-collapse-item>
            </el-collapse>

            <el-collapse v-if="!selectedPolicy" v-model="searchAdvanced" class="advanced">
              <el-collapse-item name="search" :title="t('campaignNew.advanced')">
                <div class="grid-2">
                  <el-form-item>
                    <template #label>
                      <span>{{ t('campaignNew.confirm') }}</span>
                      <InfoHint :width="360">{{ t('campaignNew.confirmHint') }}</InfoHint>
                    </template>
                    <el-input-number v-model="form.confirm_top_k" :min="0" :max="10" />
                  </el-form-item>
                  <el-form-item :label="t('campaignNew.repeats')">
                    <el-input-number v-model="form.confirm_repeats" :min="2" :max="10"
                      :disabled="form.confirm_top_k === 0" />
                  </el-form-item>
                </div>
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
                <span>{{ t('campaignNew.workload') }}</span>
                <InfoHint :width="380"><span v-html="t('campaignNew.workloadHint')" /></InfoHint>
              </template>
              <WorkloadPicker v-model="workload" v-model:rebuild="rebuildAtStart"
                :profiles="datasetProfiles" :frozen="frozenDatasets" :benchmarks="benchmarks"
                @module="(m) => (screenModule = m)" @datasets-changed="loadDatasetProfiles" />
            </el-form-item>

            <el-form-item>
              <template #label>
                <span>{{ t('campaignNew.objective') }}</span>
                <InfoHint :width="340">{{ t('campaignNew.objectiveHint') }}</InfoHint>
                <el-button link type="primary" class="label-link"
                  @click="openBuilder('/objectives')">{{ t('campaignNew.build') }}</el-button>
                <el-button link class="label-link" @click="refreshLists">{{ t('common.refresh') }}</el-button>
              </template>
              <el-select v-model="selectedObjectiveId" filterable style="width: 100%"
                :placeholder="t('campaignNew.pickObjective')">
                <el-option v-for="o in screenObjectives" :key="o.id" :value="o.id" :label="o.name">
                  <span>{{ o.name }}</span>
                  <span class="muted opt-help">{{ o.direction }} {{ o.target_metric }}</span>
                </el-option>
              </el-select>
              <div v-if="selectedObjective" class="goal-box">
                <div class="goal-line">
                  <span class="muted tiny lead-in">{{ t('campaignNew.rankBy') }}</span>
                  <b>{{ selectedObjective.direction }}</b>
                  <span class="mono">{{ selectedObjective.target_metric }}</span>
                </div>
                <!-- "must hold", not "reject if": a redline is the condition a run
                     has to SATISFY. -->
                <div v-if="selectedObjective.redlines.length" class="goal-line">
                  <span class="muted tiny lead-in">{{ t('campaignNew.mustHold') }}</span>
                  <span v-for="(r, i) in selectedObjective.redlines" :key="i" class="mono chip">
                    {{ r.metric }} {{ r.op }} {{ r.value }}
                  </span>
                </div>
                <div v-else class="muted tiny">{{ t('campaignNew.noRedlines') }}</div>
              </div>
              <div v-if="objectiveMismatch" class="stage-warn">
                {{ t('campaignNew.mismatch') }} <b>{{ selectedObjective?.target_metric }}</b>.
              </div>
            </el-form-item>

            <el-collapse v-model="advancedNames" class="advanced">
              <el-collapse-item name="split">
                <template #title>{{ t('campaignNew.verifyStage') }}</template>
                <div class="stage-box" :class="{ off: !form.verify_enabled }">
                  <div class="stage-head">
                    <el-checkbox v-model="form.verify_enabled">
                      <h4>{{ t('campaignNew.verifyTitle') }}</h4>
                    </el-checkbox>
                    <InfoHint :width="380">
                      {{ t('campaignNew.verifyHint') }}
                    </InfoHint>
                  </div>
                  <template v-if="form.verify_enabled">
                    <WorkloadPicker v-model="verifyWorkload" v-model:rebuild="rebuildAtStart"
                      :profiles="datasetProfiles" :frozen="frozenDatasets" :benchmarks="benchmarks" no-sweep
                      @module="(m) => (verifyModule = m)" @datasets-changed="loadDatasetProfiles" />
                    <div class="grid-2 verify-fields">
                      <label class="field">
                        <span class="muted tiny">Top-k</span>
                        <el-input-number v-model="form.verify_top_k" :min="1" :max="10"
                          size="small" controls-position="right" />
                      </label>
                      <label class="field">
                        <span class="muted tiny">{{ t('campaignNew.objective') }}</span>
                      <el-select v-model="verifyObjectiveId" size="small" clearable filterable
                        :placeholder="t('campaignNew.platformDefaultLc')">
                        <el-option v-for="o in verifyObjectives" :key="o.id" :value="o.id"
                          :label="o.name">
                          <span>{{ o.name }}</span>
                          <span class="muted opt-help">{{ o.target_metric }}</span>
                        </el-option>
                      </el-select>
                      </label>
                    </div>
                    <div v-if="verifyObjectiveMismatch" class="stage-warn">
                      {{ t('campaignNew.verifyMismatch') }}
                      <b>{{ selectedVerifyObjective?.target_metric }}</b>.
                    </div>
                  </template>
                </div>
              </el-collapse-item>
            </el-collapse>
          </el-form>
        </template>

        <!-- 5. Schedule -->
        <template v-if="step === 4">
          <NightlyWindow
            v-model:start="form.daily_start"
            v-model:end="form.daily_end"
            v-model:timezone="form.schedule_timezone"
            v-model:until="form.schedule_until" />
          <p v-if="nightsNeeded" class="estimate">{{ nightsNeeded }}</p>
        </template>

        <!-- 6. Check -->
        <template v-if="step === 5">
          <div class="check-head">
            <span />
            <!-- Offered only when it would actually do something. The button used
                 to show on an empty form, where `runPreflight` returns before
                 making a single call — a control that looks live, does nothing,
                 and gives no reason. -->
            <el-button v-if="checkable" size="small" :loading="checking"
              @click="runPreflight">
              {{ preflight ? t('campaignNew.rerun') : t('campaignNew.run') }}
            </el-button>
          </div>

          <div v-if="draftNotes" class="draft-notes">
            <el-alert v-for="w in draftNotes.warnings" :key="w" type="warning"
              :closable="false" show-icon :title="w" />
            <details>
              <summary class="muted tiny">
                {{ t('campaignNew.drafted', { n: Object.keys(draftNotes.provenance).length }) }}
              </summary>
              <dl class="provenance">
                <template v-for="(why, field) in draftNotes.provenance" :key="field">
                  <dt class="mono">{{ field }}</dt>
                  <dd>{{ why }}</dd>
                </template>
              </dl>
            </details>
          </div>

          <div v-if="checking" class="muted">{{ t('campaignNew.checking') }}</div>

          <el-alert v-else-if="preflight?.note" type="warning" :closable="false" show-icon
            :title="preflight.note" />

          <template v-else-if="preflight">
            <el-alert v-if="blockers.length" type="error" :closable="false" show-icon
              class="verdict"
              :title="t('campaignNew.nProblems', { n: blockers.length })" />
            <el-alert v-else type="success" :closable="false" show-icon class="verdict"
              :title="t('campaignNew.clear')" />

            <div v-for="m in preflight.machines" :key="m.machine" class="machine-checks">
              <div class="machine-name mono">{{ m.machine }}</div>
              <!-- Findings that need a decision get a line each; passes get a
                   chip, so the report is visibly complete without burying the
                   two entries that matter under seven that do not. -->
              <div v-for="c in m.checks.filter((x) => x.status !== 'pass')" :key="c.key"
                class="check" :class="c.status">
                <span class="icon">{{ statusLook[c.status]?.icon ?? '?' }}</span>
                <div class="body">
                  <div><b>{{ c.label }}</b> — {{ c.detail }}
                    <InfoHint v-if="c.hint">{{ c.hint }}</InfoHint></div>
                </div>
              </div>
              <div class="passed">
                <span v-for="c in m.checks.filter((x) => x.status === 'pass')" :key="c.key"
                  class="ok-chip" :title="c.detail">✓ {{ c.label }}</span>
              </div>
            </div>
          </template>

          <p v-else-if="!checkable" class="muted">
            {{ t('campaignNew.needs') }} <b>{{ missingForCheck.join(', ') }}</b>.
          </p>
        </template>
      </div>

      <!-- the answer so far, visible throughout rather than at a review screen -->
      <aside class="summary">
        <h3>{{ t('campaignNew.sum.title') }}</h3>
        <dl>
          <dt>{{ t('common.name') }}</dt>
          <dd>{{ form.name || defaultName }}</dd>
          <dt>{{ t('campaignNew.sum.serving') }}</dt>
          <dd :class="{ empty: !servedName }">
            {{ servedName || '—' }} <span class="muted">{{ t('campaignNew.sum.on', { engine: form.engine }) }}</span>
          </dd>
          <dt>{{ t('campaignNew.machines') }}</dt>
          <dd v-if="form.node_group">
            <el-tag size="small" type="primary" effect="plain">multi-node</el-tag>
            {{ t('campaignNew.sum.group') }} <b>{{ form.node_group }}</b>
            <span class="muted">
              ({{ t('campaignNew.nNodes', { n: groups.find((g) => g.name === form.node_group)?.node_count ?? '?' }) }})
            </span>
          </dd>
          <dd v-else>{{ form.machine_names.join(', ') || t('campaignNew.anyMachine') }}</dd>
          <dt>{{ t('campaignNew.steps.search') }}</dt>
          <dd :class="{ empty: !selectedSpace }">
            <template v-if="selectedSpace">
              {{ selectedSpace.name }}
              <span class="muted">· {{ t('campaignNew.sum.candidates', { n: selectedSpace.candidate_count }) }}
                · {{ selectedPolicy ? `policy ${selectedPolicy.name}`
                  : t('campaignNew.sum.inOrder') }}</span>
            </template>
            <template v-else>{{ t('campaignNew.sum.notChosen') }}</template>
          </dd>
          <dt>{{ t('campaignNew.sum.measuredBy') }}</dt>
          <dd>{{ describeWorkload(workload) }}</dd>
          <dt>{{ t('campaignNew.sum.wins') }}</dt>
          <dd :class="{ empty: !selectedObjective }">
            {{ selectedObjective?.name ?? t('campaignNew.sum.noObjective') }}
          </dd>
          <template v-if="staged">
            <dt>{{ t('campaignNew.sum.then') }}</dt>
            <dd>{{ t('campaignNew.sum.thenBest', { k: form.verify_top_k, workload: describeWorkload(verifyWorkload) }) }}</dd>
          </template>
          <dt>{{ t('campaignNew.sum.window') }}</dt>
          <dd :class="{ empty: !form.daily_start || !form.daily_end }">
            <template v-if="form.daily_start && form.daily_end">
              {{ form.daily_start }} → {{ form.daily_end }} {{ t('campaignNew.sum.nightly') }}
            </template>
            <template v-else>{{ t('campaignNew.sum.manual') }}</template>
          </dd>
        </dl>
        <p v-if="nightsNeeded" class="muted tiny">{{ nightsNeeded }}</p>
      </aside>
    </div>

    <el-dialog v-model="draftOpen" :title="t('campaignNew.draft.title')" width="620px">
      <el-form label-position="top">
        <el-form-item>
          <template #label>
            {{ t('campaignNew.draft.baseline') }}
            <InfoHint :width="360">{{ t('campaignNew.draft.baselineHint') }}</InfoHint>
          </template>
          <el-select v-model="draftForm.baseline_id" clearable filterable
            :placeholder="t('campaignNew.draft.pick')">
            <el-option v-for="b in baselines" :key="b.id" :value="b.id"
              :label="`#${b.id} ${b.served_model_name} · ${b.engine} · ${b.card_type || t('campaignNew.draft.anyCard')}`" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="!draftForm.baseline_id" :label="t('campaignNew.servedName')">
          <el-input v-model="draftForm.served_model_name" placeholder="served model name" />
        </el-form-item>
        <el-form-item :label="t('campaignNew.nodeGroup')">
          <el-select v-model="draftForm.node_group" clearable :placeholder="t('campaignNew.singleNode')">
            <el-option v-for="g in groups" :key="g.name" :value="g.name" :label="g.name" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('campaignNew.draft.verify')">
          <el-select v-model="draftForm.verify_benchmark_slug" clearable filterable allow-create
            default-first-option class="mono" :placeholder="t('campaignNew.draft.noSecond')">
            <el-option v-for="b in benchmarks" :key="b.slug" :value="b.slug" :label="b.slug" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="draftOpen = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="drafting"
          :disabled="!draftForm.baseline_id && !draftForm.served_model_name.trim()"
          @click="submitDraft">
          {{ t('campaignNew.draft.submit') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="fromOpen" :title="t('campaignNew.from.title')" width="560px">
      <p class="muted tiny dialog-lead">
        {{ t('campaignNew.from.lead') }}
      </p>
      <el-select v-model="fromId" filterable class="from-pick" :placeholder="t('campaignNew.from.pick')">
        <el-option v-for="c in pastCampaigns" :key="c.id" :value="c.id"
          :label="`${c.name} (#${c.id})`">
          <div class="from-option">
            <span>{{ c.name }}</span>
            <span class="muted tiny">{{ c.served_model_name }} · {{ c.status }} ·
              {{ absoluteTime(c.created_at) }}</span>
          </div>
        </el-option>
      </el-select>
      <template #footer>
        <el-button @click="fromOpen = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :disabled="fromId == null" @click="startFromCampaign(fromId!)">
          {{ t('campaignNew.from.fill') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="importOpen" :title="t('campaignNew.import.title')" width="720px">
      <el-input v-model="importText" type="textarea" :rows="16" class="mono"
        placeholder="name: ...&#10;engine: sglang&#10;image: ..." />
      <template #footer>
        <el-button @click="importOpen = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :disabled="!importText.trim()"
          @click="copiedFrom = null; applyImport()">
          {{ t('campaignNew.import.submit') }}
        </el-button>
      </template>
    </el-dialog>

    <div class="footer">
      <el-button :disabled="step === 0" @click="step--">{{ t('campaignNew.back') }}</el-button>
      <span class="grow" />
      <span v-if="problems[step].length" class="muted tiny needs">
        {{ t('campaignNew.needsLc', { items: problems[step].join(', ') }) }}
      </span>
      <el-button v-if="step < STEPS.length - 1" type="primary" @click="step++">{{ t('campaignNew.next') }}</el-button>
      <el-button v-else type="primary" :loading="busy" :disabled="!canCreate" @click="create">
        {{ t('campaignNew.create') }}
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
.estimate {
  margin: 12px 0 0;
  font-size: 12.5px;
  color: var(--el-color-primary-dark-2);
}
.opt-help {
  margin-left: 10px;
  font-size: 12px;
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
.field {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.verify-fields {
  margin-top: 10px;
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
.forbidden-default {
  margin: -4px 0 12px;
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

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
.provenance dt,
.provenance dd {
  /* The summary panel's dt/dd spacing would offset each key from its value. */
  margin: 0;
  font-size: 12px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.copied-from {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: -6px 0 10px;
}
.wide-input {
  width: 100%;
}
.from-pick {
  width: 100%;
}
.from-option {
  display: flex;
  justify-content: space-between;
  gap: 16px;
}
.dialog-lead {
  margin: 0 0 12px;
  line-height: 1.5;
}
.recipe-actions { display: flex; gap: 8px; align-items: center; margin-top: 6px; }
.recipe-summary { display: grid; gap: 6px; margin: -6px 0 16px; }
</style>
