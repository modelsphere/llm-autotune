<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  api,
  downloadText,
  fetchPolicySessionLog,
  fetchRunLog,
  type Campaign,
  type CampaignSchedule,
  type Candidate,
  type LeaderboardEntry,
  type Promotion,
  type LogSource,
  type Machine,
  type MachineLifecycle,
  type MetricSpec,
  type Policy,
  type Run,
  type RunDetail,
} from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import LogDialog from '../components/LogDialog.vue'
import NightlyWindow from '../components/NightlyWindow.vue'
import ConfigChips from '../components/ConfigChips.vue'
import CopyButton from '../components/CopyButton.vue'
import MergeRequestDialog from '../components/MergeRequestDialog.vue'
import ScoreBars from '../components/ScoreBars.vue'
import SpaceMap from '../components/SpaceMap.vue'
import { useI18n } from '../i18n'
import { beatsBaselineRatio, fmtBaselineDelta } from '../utils/baseline'
import { campaignToYaml } from '../utils/campaignYaml'
import { copyText } from '../utils/clipboard'
import { sweptKeysOf, type SpaceShape } from '../utils/space'
import { campaignStatus, isLive, runLabel, runStatus } from '../utils/status'
import { absoluteTime, duration, exactTime, relativeTime } from '../utils/time'
import { toYaml } from '../utils/yaml'

const route = useRoute()
const { t } = useI18n()
const campaignId = Number(route.params.id)

const campaign = ref<Campaign | null>(null)
const runs = ref<Run[]>([])
const candidates = ref<Candidate[]>([])
const machines = ref<Machine[]>([])
const lifecycle = ref<Record<number, MachineLifecycle>>({})
const leaderboard = ref<LeaderboardEntry[]>([])
const metricSpecs = ref<MetricSpec[]>([])
const parity = ref<{
  machine: string
  container: string
  missing: { flag: string; production: unknown; container: string }[]
} | null>(null)
const selectedRun = ref<RunDetail | null>(null)
const drawerOpen = ref(false)
const runLog = ref('')
let timer: number | undefined

/** The external policy container that searches for this campaign, when one
 *  does — the campaign row only carries its id. */
const policy = ref<Policy | null>(null)

async function load() {
  campaign.value = (await api.get(`/campaigns/${campaignId}`)).data
  if (campaign.value?.policy_id != null && policy.value?.id !== campaign.value.policy_id) {
    // The registry only lists; a deleted registration leaves the id showing.
    try {
      const all = (await api.get('/policies')).data as Policy[]
      policy.value = all.find((p) => p.id === campaign.value!.policy_id) ?? null
    } catch {
      policy.value = null
    }
  }
  runs.value = (await api.get(`/campaigns/${campaignId}/runs`)).data
  candidates.value = (await api.get(`/campaigns/${campaignId}/candidates`)).data
  machines.value = (await api.get('/machines')).data
  leaderboard.value = (await api.get(`/campaigns/${campaignId}/leaderboard`)).data
  loadPromotions()
  parity.value = (await api.get(`/campaigns/${campaignId}/parity`)).data
  const stages = (await api.get('/machines/lifecycle')).data
  lifecycle.value = Object.fromEntries(
    (stages.machines as MachineLifecycle[]).map((m) => [m.machine_id, m]),
  )
  schedule.value = (await api.get(`/campaigns/${campaignId}/schedule`)).data
}

/** The grid this campaign was created from, when it recorded one. */
const spaceName = computed(
  () => ((campaign.value?.search_space ?? {}) as { name?: string }).name ?? '')

interface DeclaredObjective {
  name?: string
  target_metric?: string
  direction?: string
  redlines?: { metric: string; op: string; value: number }[]
  // Campaigns created before the rename carry the same limits under the old
  // key; a night that already ran is shown the way it was configured.
  constraints?: { metric: string; op: string; value: number }[]
}

/** An objective, spelled out — otherwise the leaderboard's leading number is
 *  unlabelled and the reader cannot tell whether high or low is better. */
function describe(raw: unknown, fallbackKey: string) {
  const declared = (raw ?? {}) as DeclaredObjective
  const key = declared.target_metric || fallbackKey
  const spec = metricSpecs.value.find((m) => m.key === key)
  const direction = declared.direction || (spec?.better === 'lower' ? 'minimize' : 'maximize')
  return {
    // Campaigns created before objectives were named carry no name; fall back
    // to the metric rather than showing an empty label.
    name: declared.name ?? '',
    key,
    label: spec?.label ?? key,
    unit: spec?.unit ?? '',
    direction,
    redlines: declared.redlines ?? declared.constraints ?? [],
  }
}

const objective = computed(() =>
  describe(campaign.value?.objective, 'perf_guidellm_sweep.output_tpm_card_norm'))

/** The second stage's own objective. Separate because the two benchmarks share
 *  no metric names — the screening target does not exist in a replay result. */
const verifyObjective = computed(() =>
  describe(campaign.value?.verify_objective, 'replay_prod.score_card_norm'))

const staged = computed(
  () => !!campaign.value?.verify_benchmark_slug && (campaign.value?.verify_top_k ?? 0) > 0)

/** The traffic sample this campaign's replay numbers came from — the one fact
 *  that decides whether they can be compared with another campaign's at all. */
const datasetPin = computed(() => {
  const c = campaign.value
  if (!c?.dataset_build_id) return null
  return {
    profile: c.dataset_profile,
    build: c.dataset_build_id,
    adopted: c.dataset_policy_applied === 'adopted',
  }
})

/** Two tables, never one sorted list: the stages ran different benchmarks, so
 *  their scores share no scale and merging them would rank nothing. */
const verifiedBoard = computed(() => leaderboard.value.filter((e) => e.stage === 'verify'))
const screenBoard = computed(() => leaderboard.value.filter((e) => e.stage !== 'verify'))

async function stopRun(run: Run) {
  try {
    await ElMessageBox.confirm(
      `Stop run ${run.id}? Its container will be torn down and the machine released.`,
      'Stop run',
      { confirmButtonText: 'Stop it', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return // user cancelled
  }
  try {
    await api.post(`/runs/${run.id}/stop`)
    ElMessage.success('Stop requested — applied within one worker tick')
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Stop failed')
  }
}

/** What the open run was JUDGED by — the one number the search ranks on, and
 *  whether it stayed inside the redlines.
 *
 *  The drawer showed the whole metric payload as one JSON blob and nothing
 *  else, so the number that actually decided this run's fate was somewhere in
 *  the middle of a hundred keys, indistinguishable from the ninety-nine that
 *  did not matter. Which objective applies depends on the stage: a replay run
 *  is ranked on a metric the screening objective has never heard of. */
const runVerdict = computed(() => {
  const run = selectedRun.value
  if (!run) return null
  const result = [...run.results].reverse().find((r) => r.source === 'llmbench')
  if (!result) return null
  const spec = run.stage === 'verify' ? verifyObjective.value : objective.value
  return {
    stage: run.stage,
    benchmark:
      run.stage === 'verify'
        ? campaign.value?.verify_benchmark_slug
        : campaign.value?.benchmark_slug || '(platform default)',
    label: spec.label,
    key: spec.key,
    unit: spec.unit,
    direction: spec.direction,
    value: result.objective_value,
    feasible: result.feasible,
    breaches: result.breaches ?? [],
    redlines: spec.redlines,
    metrics: result.metrics ?? {},
  }
})

/** The handful of metrics an objective actually names, pulled to the front.
 *  Everything else stays available below rather than being the only view. */
const runKeyMetrics = computed(() => {
  const v = runVerdict.value
  if (!v) return []
  const wanted = [v.key, ...v.redlines.map((r) => r.metric)]
  return wanted
    .filter((k, i) => wanted.indexOf(k) === i)
    .map((key) => ({ key, value: v.metrics[key] }))
})

function fmtMetric(value: unknown): string {
  if (value === null || value === undefined) return 'not reported'
  if (typeof value === 'number') return value.toLocaleString(undefined, {
    maximumFractionDigits: 3,
  })
  return String(value)
}

const reportText = ref('')
const reportOpen = ref(false)

async function openReport() {
  reportText.value = (await api.get(`/campaigns/${campaignId}/report`)).data
  reportOpen.value = true
}

function copyReport() {
  copyText(reportText.value, t('campaign.reportCopied'))
}

// -- export ------------------------------------------------------------------

const exportOpen = ref(false)

// -- promotion: the winner as a merge request --------------------------------

const mrOpen = ref(false)
/** Which run the dialog proposes: null = the leaderboard's top. */
const mrRunId = ref<number | null>(null)
const promotions = ref<Promotion[]>([])

async function loadPromotions() {
  try {
    promotions.value = (await api.get(`/promotions?campaign_id=${campaignId}`)).data
  } catch {
    promotions.value = []
  }
}

function openMr(runId: number | null = null) {
  mrRunId.value = runId
  mrOpen.value = true
}

async function refreshPromotion(p: Promotion) {
  await api.post(`/promotions/${p.id}/refresh`)
  await loadPromotions()
}

async function cancelPromotion(p: Promotion) {
  await api.post(`/promotions/${p.id}/cancel`)
  await loadPromotions()
}

function promotionState(state: string): string {
  const key = ({
    draft: 'stateDraft', submitted: 'stateSubmitted', rolled_out: 'stateRolledOut',
    rejected: 'stateRejected', failed: 'stateFailed', cancelled: 'stateCancelled',
  } as Record<string, string>)[state]
  return key ? t(`promotion.${key}`) : state
}

/** The board's top candidate — what "Generate MR" proposes by default. */
const winner = computed(() => leaderboard.value.find((e) => !e.is_baseline) ?? null)

/** This campaign as a file. Only the inputs — status, the window it happens to
 *  be serving and any force-start override are results of running it, and a
 *  file that carried them would recreate a campaign already half-finished. */
const exportYaml = computed(() =>
  campaign.value ? campaignToYaml(campaign.value as unknown as Record<string, unknown>) : '')

function copyYaml() {
  copyText(exportYaml.value, t('campaign.yamlCopied'))
}

function downloadYaml() {
  const slug = (campaign.value?.name ?? 'campaign').replace(/[^a-z0-9]+/gi, '-').toLowerCase()
  const url = URL.createObjectURL(new Blob([exportYaml.value], { type: 'text/yaml' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `${slug}.yaml`
  link.click()
  URL.revokeObjectURL(url)
}

async function retryFailed() {
  const { data } = await api.post(`/campaigns/${campaignId}/retry-failed`)
  if (!data.retried) {
    ElMessage.info('Nothing to re-run')
    await load()
    return
  }
  // Queueing alone leaves a finished campaign finished, so the work would sit
  // there forever with no sign of why. Put it back under whatever drives it.
  const status = campaign.value?.status
  let when = 'it is already running'
  if (status === 'done' || status === 'paused') {
    const scheduled = !!schedule.value?.daily_start
    await api.put(`/campaigns/${campaignId}/status`, {
      status: scheduled ? 'scheduled' : 'active',
    })
    when = scheduled
      ? `they run in the next window (${schedule.value?.daily_start})`
      : 'starting now'
  }
  ElMessage.success(`${data.retried} config(s) re-queued — ${when}`)
  await load()
}

async function setStatus(status: 'active' | 'paused' | 'scheduled') {
  try {
    await api.put(`/campaigns/${campaignId}/status`, { status })
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Update failed')
  }
}

/** Run outside the nightly window. Needs its own endpoint rather than just
 *  setting `active`: the schedule would see no open window on the next tick
 *  and put the campaign straight back to sleep. */
async function forceStart() {
  try {
    await ElMessageBox.confirm(
      'Run this campaign now, ignoring its schedule?\n\n' +
        'It will keep going for 8 hours or until you stop it, then hand control back ' +
        'to the clock.',
      'Force start',
      { confirmButtonText: 'Run now', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  await api.post(`/campaigns/${campaignId}/force-start`, { hours: 8 })
  ElMessage.success('Running now — the first run starts within a tick')
  await load()
}

async function forceStop() {
  const live = runs.value.filter((r) => isLive(r.status)).length
  try {
    await ElMessageBox.confirm(
      live
        ? `Stop everything now? ${live} run(s) will be killed and their measurements lost.`
        : 'Pause this campaign and clear any schedule override?',
      'Force stop',
      { confirmButtonText: 'Stop everything', cancelButtonText: 'Cancel', type: 'error' },
    )
  } catch {
    return
  }
  const { data } = await api.post(`/campaigns/${campaignId}/force-stop`)
  ElMessage.success(`Paused${live ? `; ${live} run(s) stopping` : ''}`)
  void data
  await load()
}

// -- preflight ---------------------------------------------------------------

interface PreflightCheck {
  key: string
  label: string
  status: 'pass' | 'warn' | 'fail' | 'skip'
  detail: string
  hint: string
}
const preflight = ref<{
  machines: { machine: string; ok: boolean; checks: PreflightCheck[] }[]
  ok: boolean
  note: string
} | null>(null)
const checking = ref(false)

async function runPreflight() {
  checking.value = true
  try {
    preflight.value = (await api.get(`/campaigns/${campaignId}/preflight`)).data
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Could not run the checks')
  } finally {
    checking.value = false
  }
}

/** Findings that need words. Passes get a chip instead — see `preflightPassed`:
 *  hiding them entirely made "1 blocking, 1 worth reading" look like the whole
 *  report, so there was no way to tell a thorough check from a broken one. */
const preflightIssues = computed(() =>
  (preflight.value?.machines ?? []).flatMap((m) =>
    m.checks
      .filter((c) => c.status === 'fail' || c.status === 'warn')
      .map((c) => ({ machine: m.machine, ...c })),
  ),
)

const preflightPassed = computed(() =>
  (preflight.value?.machines ?? []).flatMap((m) =>
    m.checks.filter((c) => c.status === 'pass').map((c) => ({ machine: m.machine, ...c })),
  ),
)

/** Preflight answers "is it safe to start". Once a campaign is running or
 *  finished, that question has been answered by events. */
const preflightUseful = computed(() =>
  ['draft', 'scheduled', 'paused'].includes(campaign.value?.status ?? ''))

const preflightCounts = computed(() => {
  const all = (preflight.value?.machines ?? []).flatMap((m) => m.checks)
  return {
    fail: all.filter((c) => c.status === 'fail').length,
    warn: all.filter((c) => c.status === 'warn').length,
    pass: all.filter((c) => c.status === 'pass').length,
  }
})

const statusLook: Record<string, string> = {
  pass: '✓', warn: '!', fail: '✕', skip: '–',
}

// -- the clock ---------------------------------------------------------------

const schedule = ref<CampaignSchedule | null>(null)
const editingSchedule = ref(false)
const draft = ref({ start: '', end: '', timezone: '', until: null as string | null })

function openScheduleEditor() {
  draft.value = {
    start: campaign.value?.daily_start ?? '',
    end: campaign.value?.daily_end ?? '',
    timezone: campaign.value?.schedule_timezone ?? '',
    until: campaign.value?.schedule_until ?? null,
  }
  editingSchedule.value = true
}

async function saveSchedule() {
  try {
    await api.put(`/campaigns/${campaignId}/schedule`, {
      daily_start: draft.value.start,
      daily_end: draft.value.end,
      schedule_timezone: draft.value.timezone,
      schedule_until: draft.value.until,
    })
    editingSchedule.value = false
    ElMessage.success('Schedule saved — it applies from the next worker tick')
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Could not save the schedule')
  }
}

/** What the clock is about to do, in one line. Read from the backend, which
 *  owns the midnight arithmetic — a browser recomputing "is 02:00 inside last
 *  night's window" would be a second answer to a question with one. */
const scheduleLine = computed(() => {
  const s = schedule.value
  if (!s) return ''
  // An override outranks the clock, so it is what the line has to say — a
  // force-started campaign showing its nightly window would be describing
  // something that is not currently in force.
  const override = campaign.value?.override_until
  if (override && new Date(override) > new Date()) {
    return `Force-started — running until ${absoluteTime(override)}` +
      (s.daily_start ? `, then back to ${s.summary}.` : '.')
  }
  if (!s.daily_start || !s.daily_end) return 'No window — started and stopped by hand.'
  if (s.finished) return `${s.summary} — no nights left.`
  if (s.current_window) {
    return `${s.summary}. Awake now until ${absoluteTime(s.current_window.end)}.`
  }
  if (s.next_window) {
    return `${s.summary}. Next window ${relativeTime(s.next_window.start)} ` +
      `(${absoluteTime(s.next_window.start)}).`
  }
  return s.summary
})

async function openRun(run: Run) {
  selectedRun.value = (await api.get(`/runs/${run.id}`)).data
  runLog.value = await fetchRunLog(run.id)
  drawerOpen.value = true
}

function downloadRunLog() {
  const r = selectedRun.value
  if (r) downloadText(`${campaign.value?.name ?? 'campaign'}-run-${r.id}.log`, runLog.value)
}

// -- the Logs submenu: the policy container + every engine run, in one place --
const logOpen = ref(false)

const logSources = computed<LogSource[]>(() => {
  const name = campaign.value?.name ?? 'campaign'
  const sources: LogSource[] = []
  // The policy container's log (policy campaigns), found via any run's session.
  const sid = runs.value.find((r) => r.policy_session_id != null)?.policy_session_id
  if (sid != null) {
    sources.push({
      key: `policy-${sid}`,
      label: `Policy container · session ${sid}`,
      fetch: () => fetchPolicySessionLog(sid),
      filename: `${name}-policy-session-${sid}.log`,
    })
  }
  // Each engine run's log — skip external, which measures a served endpoint and
  // owns no container of its own.
  for (const r of [...runs.value].sort((a, b) => b.id - a.id)) {
    if (r.kind === 'external') continue
    sources.push({
      key: `run-${r.id}`,
      label: `Engine · run ${r.id} (${r.kind}) · ${r.status}`,
      fetch: () => fetchRunLog(r.id),
      filename: `${name}-run-${r.id}.log`,
    })
  }
  return sources
})

/** Every setting the campaign was created with, in one place — so a night can
 *  be reproduced, compared against another campaign, or have one field lifted
 *  out of it without reading the database. */
const configRows = computed(() => {
  const c = campaign.value
  if (!c) return [] as { label: string; value: string; hint?: string }[]
  const window = c.daily_start && c.daily_end
    ? `${c.daily_start} → ${c.daily_end} nightly (${c.schedule_timezone || 'platform default'})` +
      (c.schedule_until ? `, until ${absoluteTime(c.schedule_until)}` : '')
    : c.window_start || c.window_end
      ? `${c.window_start ? absoluteTime(c.window_start) : 'any time'} → ` +
        `${c.window_end ? absoluteTime(c.window_end) : 'no end'} (one-off)`
      : 'none — started and stopped by hand'
  return [
    { label: 'Engine', value: c.engine },
    { label: 'Image', value: c.image },
    { label: 'Model path', value: c.model_path, hint: 'bind-mounted to /model' },
    { label: 'Served model name', value: c.served_model_name },
    { label: 'Engine port', value: String(c.service_port) },
    {
      label: 'Benchmark',
      value: c.benchmark_slug || '(platform default)',
      hint: 'every candidate is measured by this one',
    },
    {
      label: 'Final check',
      value: staged.value
        ? `${c.verify_benchmark_slug} — best ${c.verify_top_k}, ` +
          `${c.verify_max_run_minutes} min each`
        : 'none — one benchmark decides the winner',
      hint: staged.value
        ? `ranked by ${verifyObjective.value.key}`
        : '',
    },
    ...(c.dataset_profile
      ? [{
          label: 'Replay dataset',
          value: c.dataset_build_id
            ? `${c.dataset_profile} @ ${c.dataset_build_id}`
            : `${c.dataset_profile} — not pinned yet`,
          hint: c.dataset_build_id
            ? `${c.dataset_policy_applied}; held for this campaign's whole life`
            : c.dataset_policy === 'use_current'
              ? 'takes whatever build is published when it starts'
              : 'a fresh build is requested when the campaign starts',
        }]
      : []),
    { label: 'Machines', value: (c.machine_names ?? []).join(', ') || 'any registered machine' },
    // A policy campaign has no planner: an external container decides what to
    // try, so showing "grid" here would name a search that never runs.
    ...(c.policy_id != null
      ? [{
          label: 'Policy',
          value: policy.value
            ? `${policy.value.name} — ${policy.value.image}`
            : `policy #${c.policy_id} (registration not found)`,
          hint: Object.keys(c.policy_settings ?? {}).length
            ? Object.entries(c.policy_settings)
                .map(([k, v]) => `${k}=${v}`).join(', ')
            : 'default policy settings',
        }]
      : [{ label: 'Planner', value: c.planner }]),
    { label: 'Max run minutes', value: String(c.max_run_minutes) },
    {
      label: 'Baseline canary',
      value: c.run_baseline_canary ? 'yes' : 'no',
      hint: 'benchmark production before clearing it',
    },
    {
      label: 'Machine sharing',
      value: c.share_machine ? 'yes' : 'no — one run owns the machine',
      hint: 'several candidates at once, each pinned to its own GPUs',
    },
    {
      label: 'Schedule',
      value: window,
      hint: 'when the campaign is awake; runs are cut at the window end',
    },
    {
      label: 'Current occurrence',
      value: c.window_start && c.window_end
        ? `${absoluteTime(c.window_start)} → ${absoluteTime(c.window_end)}`
        : '—',
      hint: 'written by the worker each night',
    },
    { label: 'Created', value: absoluteTime(c.created_at) },
  ]
})

/** The hand-written blocks, as YAML — these are the ones worth copying into
 *  the next campaign verbatim. */
const extrasYaml = computed(() =>
  toYaml({
    env: campaign.value?.extra_env ?? {},
    volumes: campaign.value?.extra_volumes ?? {},
  }),
)
const searchSpaceYaml = computed(() => toYaml(campaign.value?.search_space ?? {}))
const objectiveYaml = computed(() => toYaml(campaign.value?.objective ?? {}))

const hasExtras = computed(
  () =>
    Object.keys(campaign.value?.extra_env ?? {}).length > 0 ||
    Object.keys(campaign.value?.extra_volumes ?? {}).length > 0,
)

/** Everything at once, in the shape the New campaign dialog understands. */
function copyEverything() {
  const c = campaign.value
  if (!c) return
  copyText(
    toYaml({
      name: c.name,
      engine: c.engine,
      image: c.image,
      model_path: c.model_path,
      served_model_name: c.served_model_name,
      service_port: c.service_port,
      benchmark_slug: c.benchmark_slug,
      machine_names: c.machine_names,
      max_run_minutes: c.max_run_minutes,
      run_baseline_canary: c.run_baseline_canary,
      planner: c.planner,
      extra_env: c.extra_env,
      extra_volumes: c.extra_volumes,
      search_space: c.search_space,
      objective: c.objective,
    }),
    'Full configuration copied as YAML',
  )
}

const space = computed<SpaceShape>(() => (campaign.value?.search_space ?? {}) as SpaceShape)

/** What this campaign sweeps, straight from the space it was created with —
 *  read by the same function every other view of a space uses. This page once
 *  had its own copy that never looked at `range`, so a range-based sweep
 *  rendered twenty distinct configurations as two repeated lines. */
const sweptKeys = computed<string[]>(() => sweptKeysOf(space.value))

const baseConfig = computed<Record<string, unknown>>(() => space.value.base ?? {})

/** What became of a candidate, in the words of what actually happened to it:
 *  the run is authoritative, the candidate's own status only says whether the
 *  planner still owes it a run. */
function candidateOutcome(candidate: Candidate) {
  if (candidate.validation_error) {
    return { label: 'rejected', type: 'danger', detail: candidate.validation_error }
  }
  const run = runs.value.find((r) => r.candidate_id === candidate.id) // newest first
  if (!run) {
    return candidate.status === 'valid'
      ? { label: 'queued', type: 'info', detail: 'waiting for a free machine' }
      : { label: candidate.status, type: 'info', detail: '' }
  }
  const detail = [`run ${run.id}`, run.failure_class].filter(Boolean).join(' · ')
  if (run.status === 'succeeded') return { label: 'benchmarked', type: 'success', detail }
  if (run.status === 'failed') return { label: 'failed', type: 'danger', detail }
  if (run.status === 'killed') return { label: 'stopped', type: 'warning', detail }
  return { label: runLabel(run.status), type: 'primary', detail }
}

/** How many full-machine passes this campaign needs — the same arithmetic the
 *  scheduler does, so the estimate cannot drift from what actually happens. */
const machineCards = computed(() => {
  const pinned = campaign.value?.machine_names ?? []
  const usable = machines.value.filter((m) => !pinned.length || pinned.includes(m.name))
  return Math.max(0, ...usable.map((m) => m.gpu_count))
})

/** How big the search space IS, from the campaign's own declaration.
 *
 *  Not the number of candidate rows: the planner expands the space only once
 *  the campaign is active, so a scheduled or paused campaign has none, and
 *  showing that count said a 48-config sweep was empty. Computed on the
 *  server, where conditions are applied — multiplying the axes here would
 *  over-count every gated parameter. */
const spaceSize = computed(() => campaign.value?.candidate_count ?? null)

const packedRounds = computed(() => {
  const cards = candidates.value
    .filter((c) => !c.config.__baseline__)
    .map((c) => Math.max(1, c.cards))
  if (!cards.length) return 0
  if (machineCards.value <= 0) return cards.length
  return Math.max(1, Math.ceil(cards.reduce((a, b) => a + b, 0) / machineCards.value))
})

/** How long the night is, in machine passes rather than in runs. */
const packingNote = computed(() => {
  if (!campaign.value?.share_machine || !packedRounds.value) return ''
  return `${packedRounds.value} round(s) of ${machineCards.value} cards, widest first`
})

function rowClass({ row }: { row: Candidate }): string {
  return runs.value.some((r) => r.candidate_id === row.id) ? 'clickable' : ''
}

function openCandidateRun(candidate: Candidate) {
  const run = runs.value.find((r) => r.candidate_id === candidate.id)
  if (run) openRun(run)
}

/** 68586.56244036907 is twelve digits of a number whose last six are noise. */
function fmtScore(value: number | null): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 })
}

/** A candidate versus the same-stage production baseline, as a signed
 *  percentage — "+2.0%" better, "-6.2%" worse — the same convention the
 *  ScoreBars use. Screening and replay both read it off `vs_baseline`, so the
 *  two boards no longer show the comparison two different ways. */
const fmtVsBaseline = fmtBaselineDelta
const beatsBaseline = beatsBaselineRatio

/** Did this run land on a different card than the baseline it is measured
 *  against? Per-card throughput is card-COUNT normalized, not card-TYPE, so an
 *  off-chip run carries no vs_baseline and ranks below the same-chip entries —
 *  the tag says why it is not being compared. */
function offChip(row: LeaderboardEntry): boolean {
  if (row.is_baseline || !row.card_type) return false
  const baseline = leaderboard.value.find((e) => e.is_baseline && e.stage === row.stage)
  return !!baseline?.card_type && baseline.card_type !== row.card_type
}

const progress = computed(() => {
  // The denominator is the LARGER of the declared space size and the candidate
  // rows created so far — not the row count alone. The grid planner materializes
  // candidates in batches (max_new per pass), so mid-search there are fewer rows
  // than the space will produce, and dividing by them read a 256-config sweep as
  // "101/205" — a moving denominator still being planned. Verify repeats, on the
  // other hand, push the real total PAST the declared size (48 screening + 3
  // replays is 51 runs), so once rows exceed it they win. The max satisfies both:
  // the declared size is a floor while planning, the row count takes over once
  // repeats grow past it. Before planning both fall back and it reads 0/256.
  const total = Math.max(candidates.value.length, spaceSize.value ?? 0) || spaceSize.value
  const done = runs.value.filter((r) => !isLive(r.status)).length
  return { total, done }
})

/** Why is an active campaign not starting anything? Silence is the worst
 *  answer — the scheduler's preconditions must be visible.
 *
 *  What each machine is waiting for comes from `/machines/lifecycle`, the same
 *  answer the Resources page shows. This used to be re-derived here from
 *  `state` and `baseline_status`, in four paragraphs that could disagree with
 *  the page an operator would go and look at next. */
const stall = computed<{ title: string; lines: string[] } | null>(() => {
  const c = campaign.value
  if (!c || c.status !== 'active') return null
  if (!candidates.value.some((x) => x.status === 'valid')) return null
  if (runs.value.some((r) => isLive(r.status))) return null

  const pinned = c.machine_names ?? []
  const usable = machines.value.filter((m) => !pinned.length || pinned.includes(m.name))
  if (!usable.length) {
    return {
      title: 'Pinned to a machine that is not registered',
      lines: [`${pinned.join(', ')} — add it on the Resources page.`],
    }
  }
  return {
    title: 'Active, but nothing is running',
    lines: usable.map((m) => {
      const stage = lifecycle.value[m.id]
      return stage ? `${m.name} — ${stage.headline}. ${stage.detail}` : `${m.name} — ${m.state}`
    }),
  }
})

/** Configurations worth another attempt: every run of them failed.
 *
 *  Counted the way the server re-queues them — per CONFIGURATION, not per run —
 *  so the button can say how much work it is about to create. A config with one
 *  failure and one success is already measured and is left alone. */
const retryableCount = computed(() => {
  if (runs.value.some((r) => isLive(r.status))) return 0
  const byCandidate = new Map<number, string[]>()
  for (const run of runs.value) {
    byCandidate.set(run.candidate_id, [...(byCandidate.get(run.candidate_id) ?? []), run.status])
  }
  let n = 0
  for (const statuses of byCandidate.values()) {
    if (statuses.length && statuses.every((st) => st === 'failed' || st === 'killed')) n += 1
  }
  return n
})

onMounted(() => {
  load()
  api.get('/objectives/metrics').then(({ data }) => (metricSpecs.value = data.metrics))
  timer = window.setInterval(load, 10000) // polling for PoC; SSE later
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <div class="page" v-if="campaign">
    <div class="header-row">
      <div>
        <h1 class="page-title">{{ campaign.name }}</h1>
        <span class="muted">
          {{ campaign.engine }} · {{ campaign.served_model_name }} ·
          <template v-if="campaign.policy_id != null">
            {{ t('campaign.policy') }}: {{ policy?.name ?? `#${campaign.policy_id}` }} ·
          </template>
          <template v-else>{{ t('campaign.planner') }}: {{ campaign.planner }} ·</template>
          {{ t('campaign.evaluated', { done: progress.done, total: progress.total }) }}
          <template v-if="spaceName"><br />{{ t('campaign.grid') }}: <b>{{ spaceName }}</b></template>
          <template v-if="objective.name"> · {{ t('campaign.objective') }}:
            <b>{{ objective.name }}</b></template>
        </span>
      </div>
      <div>
        <el-tag size="large" :type="campaignStatus(campaign.status).type"
          :class="campaignStatus(campaign.status).cls" style="margin-right: 12px">
          {{ campaign.status }}
        </el-tag>
        <el-button @click="openReport">{{ t('campaign.report') }}</el-button>
        <el-button v-if="logSources.length" @click="logOpen = true">
          {{ t('campaign.logs') }}
        </el-button>
        <el-button @click="exportOpen = true">
          {{ t('campaign.exportYaml') }}
          <InfoHint :width="320">
            Everything needed to recreate this campaign. Import it on the New campaign
            page to build a variant without walking the whole form again.
          </InfoHint>
        </el-button>
        <el-button type="success" plain :disabled="!winner" @click="openMr(null)">
          {{ t('promotion.generateMr') }}
          <InfoHint :width="340">
            The leaderboard's top configuration as a merge request against the deploy
            repo file the baseline is bound to: a knob-level diff you review before
            anything is opened. Per-row buttons on the board propose a specific run.
          </InfoHint>
        </el-button>

        <!-- Named for what it does and how much of it there is. "Retry failed"
             said neither, so nobody could tell whether it re-ran one config or
             the whole night. It re-queues only candidates whose every run
             failed, and then puts the campaign back to work — a button that
             queued work but left the campaign DONE did nothing visible. -->
        <el-button v-if="retryableCount" @click="retryFailed">
          {{ retryableCount === 1
            ? t('campaign.rerunFailedOne', { n: retryableCount })
            : t('campaign.rerunFailedMany', { n: retryableCount }) }}
          <InfoHint :width="330">
            Configurations whose every attempt failed go back in the queue. Ones that
            succeeded are left alone, so nothing already measured is thrown away or
            measured twice.
          </InfoHint>
        </el-button>

        <!-- Pause is gentle: no new runs, current ones finish. Force stop kills
             them. Both are offered because "stop" means different things when a
             benchmark is 18 minutes into 20. -->
        <template v-if="campaign.status === 'active'">
          <el-button type="warning" @click="setStatus('paused')">
            {{ t('campaign.pause') }}
          </el-button>
          <el-button type="danger" plain @click="forceStop">
            {{ t('campaign.forceStop') }}
          </el-button>
        </template>
        <template v-else-if="campaign.status === 'scheduled'">
          <el-button type="primary" @click="forceStart">
            {{ t('campaign.forceStart') }}
          </el-button>
          <el-button type="warning" plain @click="setStatus('paused')">
            {{ t('campaign.pause') }}
          </el-button>
        </template>
        <template v-else-if="campaign.status === 'paused' && schedule?.daily_start">
          <el-button type="primary" @click="setStatus('scheduled')">
            {{ t('campaign.resumeSchedule') }}
          </el-button>
          <el-button @click="forceStart">{{ t('campaign.forceStart') }}</el-button>
        </template>
        <!-- A policy campaign with no window cannot be activated (its session
             deadlines come from the window), so Force start is the button:
             it writes an 8h window and activates in one call. -->
        <template v-else-if="(campaign.status === 'draft' || campaign.status === 'paused')
          && campaign.policy_id != null && !campaign.window_end">
          <el-button type="primary" @click="forceStart">
            {{ t('campaign.forceStart') }}
          </el-button>
        </template>
        <el-button v-else-if="campaign.status === 'draft' || campaign.status === 'paused'"
          type="primary" @click="setStatus('active')">
          {{ t('campaign.start') }}
        </el-button>
        <!-- `done` deliberately offers no Start: the search is exhausted, so
             activating it plans nothing, finds nothing to place, and the very
             next tick marks it done again. The honest actions are reading the
             report and re-running whatever failed. -->
      </div>
    </div>

    <!-- Lease readiness of the pinned machines, before a run is placed. -->
    <el-alert
      v-for="w in (campaign.machine_warnings ?? [])"
      :key="w.machine + w.status"
      :type="w.status === 'expiring' ? 'warning' : 'error'"
      :closable="false"
      show-icon
      class="warn-banner"
      :title="w.detail"
    />

    <div class="clock">
      <span class="muted">{{ scheduleLine }}</span>
      <el-button link type="primary" size="small" @click="openScheduleEditor">
        {{ schedule?.daily_start ? t('campaign.editSchedule') : t('campaign.addSchedule') }}
      </el-button>
    </div>

    <!-- Every pre-run finding in one place, run on demand rather than as a
         banner. Parity used to live here as its own alert and read as an error
         nobody could act on; it is one check among several now, alongside the
         ones that actually stop a run. -->
    <!-- Only while a start is still ahead. On a running campaign the port
         check reports our OWN engine holding the port, and on a finished one
         every answer is about a machine the campaign is no longer using —
         findings that look like problems and are not. -->
    <div v-if="preflightUseful" class="preflight-bar">
      <span v-if="!preflight" class="muted">
        Checks not run yet — worth doing before a start; the machine may have changed.
      </span>
      <span v-else class="muted counts">
        <b v-if="preflightCounts.fail" class="fail-text">
          {{ preflightCounts.fail }} blocking
        </b>
        <b v-if="preflightCounts.warn" class="warn-text">
          {{ preflightCounts.warn }} worth reading
        </b>
        <b class="ok-text">{{ preflightCounts.pass }} passed</b>
      </span>
      <span class="spacer" />
      <el-button size="small" :loading="checking" @click="runPreflight">
        {{ preflight ? 'Re-check' : 'Run pre-flight checks' }}
      </el-button>
    </div>

    <div v-if="preflight" class="preflight-list">
      <div v-for="(c, i) in preflightIssues" :key="i" class="check" :class="c.status">
        <span class="icon">{{ statusLook[c.status] }}</span>
        <div>
          <div>
            <span class="mono muted">{{ c.machine }}</span>
            <b> {{ c.label }}</b> — {{ c.detail }}
          </div>
          <div v-if="c.hint" class="muted tiny">{{ c.hint }}</div>
        </div>
      </div>
      <!-- Passes as chips: present, countable, and not competing for attention
           with the ones that need a decision. -->
      <div v-if="preflightPassed.length" class="passed">
        <span v-for="(c, i) in preflightPassed" :key="i" class="ok-chip"
          :title="`${c.machine}: ${c.detail}`">
          ✓ {{ c.label }}
        </span>
      </div>
    </div>

    <el-alert v-if="stall" type="warning" :closable="false" :title="stall.title"
      style="margin-bottom: 12px" show-icon>
      <template #default>
        <div v-for="(line, i) in stall.lines" :key="i" class="stall-line">{{ line }}</div>
      </template>
    </el-alert>

    <!-- An unattended action has to be visible before it happens, not only
         after: this is the campaign saying what it will do when it finishes. -->
    <div v-if="campaign?.auto_promote" class="promotions">
      <el-tag size="small" type="warning" effect="plain">{{ t('promotion.autoOn') }}</el-tag>
      <span class="muted tiny">
        {{ campaign.deploy_branch
          ? t('promotion.autoOnBranch', { branch: campaign.deploy_branch })
          : t('promotion.autoOnHint') }}
      </span>
    </div>

    <div v-if="promotions.length" class="promotions">
      <span class="muted tiny">{{ t('promotion.history') }}:</span>
      <span v-for="p in promotions" :key="p.id" class="promotion">
        <el-tag size="small" effect="plain"
          :type="p.state === 'rolled_out' ? 'success' : p.state === 'failed' || p.state === 'rejected' ? 'danger' : 'info'">
          #{{ p.id }} · run {{ p.run_id }} · {{ promotionState(p.state) }}
        </el-tag>
        <a v-if="p.refs?.mr_url" :href="String(p.refs.mr_url)" target="_blank" rel="noopener" class="tiny">
          {{ t('promotion.viewMr') }}</a>
        <span v-else-if="p.refs?.branch" class="mono tiny muted">{{ p.refs.branch }}</span>
        <span v-if="p.error" class="tiny muted" :title="p.error">⚠</span>
        <el-button v-if="!['rolled_out', 'rejected', 'failed', 'cancelled'].includes(p.state)" size="small" link
          @click="refreshPromotion(p)">{{ t('promotion.refresh') }}</el-button>
        <el-button v-if="!['rolled_out', 'rejected', 'failed', 'cancelled'].includes(p.state)" size="small" link
          type="danger" @click="cancelPromotion(p)">{{ t('promotion.cancel') }}</el-button>
      </span>
    </div>

    <MergeRequestDialog v-model="mrOpen" :preview-url="`/campaigns/${campaignId}/promote/preview`"
      :promote-url="`/campaigns/${campaignId}/promote`"
      :body="mrRunId ? { run_id: mrRunId } : {}" @promoted="loadPromotions" />

    <el-tabs>
      <el-tab-pane :label="t('campaign.tabs.leaderboard')">
        <!-- The expensive stage first, in its own table: it decides the winner,
             and its scores come from a different benchmark in different units.
             One table sorted across both would be a ranking of nothing. -->
        <template v-if="verifiedBoard.length">
          <div class="board-head">
            <h3 class="board-title">{{ t('campaign.verifiedOn') }}</h3>
            <span class="mono muted">{{ campaign?.verify_benchmark_slug }}</span>
          </div>
          <p class="muted objective-line">
            <b>{{ verifyObjective.direction }} {{ verifyObjective.label }}</b>
            <span class="mono key">{{ verifyObjective.key }}</span>
            <InfoHint>
              Production was measured with the screening benchmark, not this one, so there
              is no like-for-like comparison against it here.
            </InfoHint>
          </p>
          <p v-if="datasetPin" class="muted objective-line">
            <span>{{ t('campaign.replayedOn') }}</span>
            <span class="mono key">{{ datasetPin.build }}</span>
            <el-tag v-if="datasetPin.adopted" size="small" type="info" effect="plain">
              {{ t('campaign.datasetAdopted') }}
            </el-tag>
            <InfoHint>
              Every candidate here replayed this one build, so their scores compare.
              Another campaign's numbers only compare to these if it replayed the same
              build id.
            </InfoHint>
          </p>
          <ScoreBars :rows="verifiedBoard" :swept-keys="sweptKeys"
            :label="verifyObjective.label" :unit="verifyObjective.unit"
            :direction="verifyObjective.direction" />
          <el-table :data="verifiedBoard">
            <el-table-column prop="run_id" :label="t('campaign.run')" width="80" />
            <el-table-column :label="t('common.config')" min-width="280">
              <template #default="{ row }">
                <ConfigChips :config="row.config" :keys="sweptKeys" />
              </template>
            </el-table-column>
            <el-table-column label="GPU" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.card_type" size="small" effect="plain"
                  :type="offChip(row) ? 'warning' : 'info'"
                  :title="offChip(row)
                    ? 'Ran on a different card than the baseline — not comparable per-card'
                    : ''">{{ row.card_type }}</el-tag>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="score" :label="verifyObjective.label" width="180" sortable>
              <template #default="{ row }">
                <el-tag v-if="row.is_baseline" size="small" type="info" effect="plain"
                  class="baseline-tag">{{ t('campaign.baselineRow') }}</el-tag>
                {{ fmtScore(row.score) }}
              </template>
            </el-table-column>
            <el-table-column :label="t('campaign.vsBaseline')" width="110">
              <template #default="{ row }">
                <span v-if="row.is_baseline" class="muted">—</span>
                <span v-else-if="row.vs_baseline != null"
                  :class="beatsBaseline(row.vs_baseline, verifyObjective.direction) ? 'better' : 'worse'">
                  {{ fmtVsBaseline(row.vs_baseline, verifyObjective.direction) }}
                </span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('campaign.redlines')" min-width="200">
              <template #default="{ row }">
                <el-tag v-if="!row.comparable" type="warning" size="small"
                  :title="row.dataset_build_id">
                  {{ t('campaign.otherDataset') }}
                </el-tag>
                <el-tag v-else-if="row.holds_redlines" type="success" size="small">{{ t('campaign.held') }}</el-tag>
                <template v-else>
                  <el-tag type="danger" size="small">{{ t('campaign.crossed') }}</el-tag>
                  <span class="muted breach">{{ row.breaches.join('; ') }}</span>
                </template>
              </template>
            </el-table-column>
            <el-table-column label="" width="110">
              <template #default="{ row }">
                <el-button v-if="!row.is_baseline" size="small" link type="primary" @click="openMr(row.run_id)">
                  {{ t('promotion.generateMr') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
          <h3 class="board-title screening">{{ t('campaign.screening') }}</h3>
        </template>

        <p class="muted objective-line">
          <b>{{ objective.direction }} {{ objective.label }}</b>
          <span class="mono key">{{ objective.key }}</span>
          <template v-if="objective.redlines.length">
            <span class="sep">·</span>
            <span v-for="(r, i) in objective.redlines" :key="i" class="mono">
              {{ i ? ', ' : '' }}{{ r.metric }} {{ r.op }} {{ r.value }}</span>
            <InfoHint>
              Redlines. A config that crosses one is rejected however fast it was — and a
              metric the benchmark never reported counts as crossed, because we cannot
              certify an SLO we did not measure.
            </InfoHint>
          </template>
        </p>
        <p v-if="staged && !verifiedBoard.length" class="muted tiny pending-note">
          The best {{ campaign?.verify_top_k }} of these get replayed against
          <span class="mono">{{ campaign?.verify_benchmark_slug }}</span> once screening
          finishes — that measurement decides the winner.
        </p>
        <ScoreBars :rows="screenBoard" :swept-keys="sweptKeys"
          :label="objective.label" :unit="objective.unit"
          :direction="objective.direction" />
        <el-table :data="screenBoard">
          <el-table-column prop="run_id" :label="t('campaign.run')" width="80" />
          <el-table-column :label="t('common.config')" min-width="280">
            <template #default="{ row }">
              <ConfigChips :config="row.config" :keys="sweptKeys" />
            </template>
          </el-table-column>
          <el-table-column label="GPU" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.card_type" size="small" effect="plain"
                :type="offChip(row) ? 'warning' : 'info'"
                :title="offChip(row)
                  ? 'Ran on a different card than the baseline — not comparable per-card'
                  : ''">{{ row.card_type }}</el-tag>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="score" :label="objective.unit
            ? `${objective.label} (${objective.unit})` : objective.label"
            width="170" sortable>
            <template #default="{ row }">
              <el-tag v-if="row.is_baseline" size="small" type="info" effect="plain"
                class="baseline-tag">{{ t('campaign.baselineRow') }}</el-tag>
              {{ fmtScore(row.score) }}
            </template>
          </el-table-column>
          <el-table-column :label="t('campaign.vsBaseline')" width="110">
            <template #default="{ row }">
              <span v-if="row.is_baseline" class="muted">—</span>
              <span v-else-if="row.vs_baseline != null"
                :class="beatsBaseline(row.vs_baseline, objective.direction) ? 'better' : 'worse'">
                {{ fmtVsBaseline(row.vs_baseline, objective.direction) }}
              </span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('campaign.redlines')" min-width="200">
            <template #default="{ row }">
              <el-tag v-if="row.holds_redlines" type="success" size="small">{{ t('campaign.held') }}</el-tag>
              <template v-else>
                <el-tag type="danger" size="small">{{ t('campaign.crossed') }}</el-tag>
                <span class="muted breach">{{ row.breaches.join('; ') }}</span>
              </template>
            </template>
          </el-table-column>
          <el-table-column label="" width="110">
            <template #default="{ row }">
              <el-button v-if="!row.is_baseline" size="small" link type="primary" @click="openMr(row.run_id)">
                {{ t('promotion.generateMr') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="screenBoard.length === 0" class="muted">{{ t('campaign.noRuns') }}</p>
      </el-tab-pane>

      <el-tab-pane :label="t('campaign.tabs.runs')">
        <el-table :data="runs" @row-click="openRun" style="cursor: pointer">
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column label="Status" width="140">
            <template #default="{ row }">
              <el-tag :type="runStatus(row.status).type" size="small">
                {{ runLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <!-- Which benchmark this run was measured by. Without it, a replay
               run and a sweep run are two indistinguishable rows whose numbers
               differ by orders of magnitude. -->
          <el-table-column v-if="staged" label="Test" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.stage === 'verify'" type="warning" size="small" effect="plain">
                replay
              </el-tag>
              <span v-else class="muted tiny">screen</span>
            </template>
          </el-table-column>
          <el-table-column prop="failure_class" label="Failure" width="120" />
          <el-table-column label="GPUs" width="110">
            <template #default="{ row }">
              <span class="mono">{{ row.gpu_indices?.length ? row.gpu_indices.join(',') : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="endpoint_url" label="Endpoint" min-width="180" class-name="mono" />
          <el-table-column label="Started" min-width="130">
            <template #default="{ row }">
              <span :title="exactTime(row.started_at)">{{ relativeTime(row.started_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="Took" width="90">
            <template #default="{ row }">{{ duration(row.started_at, row.finished_at) }}</template>
          </el-table-column>
          <el-table-column label="" width="90">
            <template #default="{ row }">
              <el-button v-if="isLive(row.status)" size="small" type="danger" plain
                @click.stop="stopRun(row)">
                Stop
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="t('campaign.tabs.configuration')">
        <div class="config-head">
          <span class="muted">
            A snapshot, taken when the campaign was created.
            <InfoHint>
              Editing the search space or objective this was copied from does not change
              what ran here — a campaign always matches the config it was created with.
            </InfoHint>
          </span>
          <el-button size="small" @click="copyEverything">Copy all as YAML</el-button>
        </div>

        <el-table :data="configRows" size="small" class="config-table">
          <el-table-column label="Setting" width="230">
            <template #default="{ row }">
              <div>{{ row.label }}</div>
              <div v-if="row.hint" class="muted tiny">{{ row.hint }}</div>
            </template>
          </el-table-column>
          <el-table-column label="Value" min-width="320">
            <template #default="{ row }"><span class="mono">{{ row.value }}</span></template>
          </el-table-column>
          <!-- An icon, not a word: the label was repeated in every row to say
               what one glyph says once, and it cost a column doing it. -->
          <el-table-column width="48" align="center">
            <template #default="{ row }">
              <CopyButton :value="row.value" :label="`${row.label} copied`" />
            </template>
          </el-table-column>
        </el-table>

        <div class="block-head">
          <h3>Launch extras</h3>
          <span class="muted tiny">
            env vars and bind mounts every container in this campaign gets
          </span>
          <span class="spacer" />
          <el-button size="small" text type="primary"
            @click="copyText(extrasYaml, 'Launch extras copied')">Copy YAML</el-button>
        </div>
        <pre class="mono block">{{ hasExtras ? extrasYaml : '(none)' }}</pre>

        <div class="block-head">
          <h3>Difference from production</h3>
          <span class="muted tiny">
            flags <span class="mono">{{ parity?.container || 'the captured service' }}</span>
            passes that this campaign never sets
          </span>
        </div>
        <pre class="mono block">{{ parity?.missing?.length
          ? parity.missing.map((m) => `${m.flag}: ${m.production}`).join('\n')
          : (parity?.machine ? 'none — every flag production sets is set here too'
                             : 'no captured production service to compare against') }}</pre>

        <div class="block-head">
          <h3>Search space</h3>
          <span class="muted tiny">{{ spaceName || 'written for this campaign' }}</span>
          <span class="spacer" />
          <el-button size="small" text type="primary"
            @click="copyText(searchSpaceYaml, 'Search space copied')">Copy YAML</el-button>
        </div>
        <pre class="mono block">{{ searchSpaceYaml }}</pre>

        <div class="block-head">
          <h3>Objective</h3>
          <span class="muted tiny">{{ objective.name || 'written for this campaign' }}</span>
          <span class="spacer" />
          <el-button size="small" text type="primary"
            @click="copyText(objectiveYaml, 'Objective copied')">Copy YAML</el-button>
        </div>
        <pre class="mono block">{{ objectiveYaml }}</pre>
      </el-tab-pane>

      <el-tab-pane :label="t('campaign.tabs.candidates')">
        <SpaceMap :space="space" :candidates="spaceSize" :note="packingNote"
          class="space-map" />
        <p v-if="!candidates.length && spaceSize" class="muted tiny not-planned">
          {{ t('campaign.notPlanned', { n: spaceSize }) }}
        </p>

        <el-table :data="candidates" @row-click="openCandidateRun"
          :row-class-name="rowClass">
          <el-table-column prop="id" label="#" width="70" />
          <el-table-column :label="t('campaign.sweptParameters')" min-width="340">
            <template #default="{ row }">
              <ConfigChips :config="row.config" :keys="sweptKeys" :cards="row.cards" />
            </template>
          </el-table-column>
          <el-table-column label="Status" min-width="260">
            <template #default="{ row }">
              <el-tag :type="candidateOutcome(row).type" size="small">
                {{ candidateOutcome(row).label }}
              </el-tag>
              <span class="muted breach">{{ candidateOutcome(row).detail }}</span>
            </template>
          </el-table-column>
        </el-table>

        <el-collapse v-if="Object.keys(baseConfig).length" class="viewer">
          <el-collapse-item title="Fixed in every candidate">
            <pre class="mono block">{{ toYaml(baseConfig) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="editingSchedule" title="Nightly window" width="560px">
      <p class="muted lead">
        The campaign wakes at the start time and stands down at the end, picking up where
        it left off the next night. Clearing both times puts it back under manual control.
      </p>
      <NightlyWindow v-model:start="draft.start" v-model:end="draft.end"
        v-model:timezone="draft.timezone" v-model:until="draft.until" />
      <template #footer>
        <el-button @click="editingSchedule = false">Cancel</el-button>
        <el-button type="primary" @click="saveSchedule">Save</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="reportOpen" :title="t('campaign.reportTitle')" width="760px">
      <pre class="mono block report">{{ reportText }}</pre>
      <template #footer>
        <el-button @click="copyReport">{{ t('campaign.copyMarkdown') }}</el-button>
        <el-button type="primary" @click="reportOpen = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="exportOpen" :title="t('campaign.exportTitle')" width="760px">
      <p class="muted tiny">
        {{ t('campaign.exportNote') }}
      </p>
      <pre class="mono block report">{{ exportYaml }}</pre>
      <template #footer>
        <el-button @click="downloadYaml">{{ t('common.download') }}</el-button>
        <el-button @click="copyYaml">{{ t('common.copy') }}</el-button>
        <el-button type="primary" @click="exportOpen = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="drawerOpen" :title="`Run ${selectedRun?.id}`" size="50%"
      :with-header="true">
      <template v-if="selectedRun">
        <div v-if="runVerdict" class="verdict-box">
          <div class="goal-line">
            <span class="muted tiny lead-in">measured by</span>
            <el-tag size="small" effect="plain"
              :type="runVerdict.stage === 'verify' ? 'warning' : 'info'">
              {{ runVerdict.stage === 'verify' ? 'replay' : (staged ? 'screening' : 'benchmark') }}
            </el-tag>
            <span class="mono muted">{{ runVerdict.benchmark }}</span>
          </div>
          <div class="goal-line">
            <span class="muted tiny lead-in">ranked on</span>
            <span class="mono">{{ runVerdict.key }}</span>
          </div>
          <div class="goal-line">
            <span class="muted tiny lead-in">score</span>
            <b class="headline">{{ fmtScore(runVerdict.value) }}</b>
            <span v-if="runVerdict.unit" class="muted tiny">{{ runVerdict.unit }}</span>
            <span class="muted tiny">({{ runVerdict.direction }})</span>
          </div>
          <div class="goal-line">
            <span class="muted tiny lead-in">redlines</span>
            <el-tag v-if="runVerdict.feasible" type="success" size="small">{{ t('campaign.held') }}</el-tag>
            <template v-else>
              <el-tag type="danger" size="small">{{ t('campaign.crossed') }}</el-tag>
              <span class="muted tiny">{{ runVerdict.breaches.join('; ') || 'no score' }}</span>
            </template>
          </div>
        </div>

        <h3 v-if="runKeyMetrics.length">What the objective looked at</h3>
        <div v-if="runKeyMetrics.length" class="key-metrics">
          <div v-for="m in runKeyMetrics" :key="m.key" class="metric-row">
            <span class="mono muted">{{ m.key }}</span>
            <span class="mono val">{{ fmtMetric(m.value) }}</span>
          </div>
        </div>

        <h3>Config</h3>
        <pre class="mono block">{{ JSON.stringify(selectedRun.config, null, 2) }}</pre>
        <h3>Launch command</h3>
        <pre class="mono block">{{ selectedRun.launch_command || '(not launched yet)' }}</pre>
        <h3>Results</h3>
        <el-table :data="selectedRun.results">
          <el-table-column prop="source" label="Source" width="110" />
          <el-table-column label="Passed" width="90">
            <template #default="{ row }">{{ row.passed ? 'yes' : 'no' }}</template>
          </el-table-column>
          <el-table-column label="Metrics" min-width="260">
            <template #default="{ row }">
              <el-collapse v-if="Object.keys(row.metrics ?? {}).length">
                <el-collapse-item
                  :title="`${Object.keys(row.metrics).length} metrics`">
                  <pre class="mono block small">{{ JSON.stringify(row.metrics, null, 2) }}</pre>
                </el-collapse-item>
              </el-collapse>
              <span v-else class="muted tiny">none</span>
            </template>
          </el-table-column>
        </el-table>
        <h3>Error</h3>
        <pre class="mono block">{{ selectedRun.error || '(none)' }}</pre>
        <div class="log-head">
          <h3>Captured log</h3>
          <el-button v-if="runLog" size="small" @click="downloadRunLog">
            {{ t('common.download') }}
          </el-button>
        </div>
        <pre class="mono block log">{{ runLog }}</pre>
      </template>
    </el-drawer>

    <LogDialog v-model="logOpen" :title="`${campaign?.name ?? ''} — ${t('campaign.logs')}`"
      :sources="logSources" />
  </div>
</template>

<style scoped>
.log-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.chip {
  display: inline-flex;
  align-items: stretch;
  margin: 2px 8px 2px 0;
  border: 1px solid var(--autotune-border, #dcdfe6);
  border-radius: 5px;
  overflow: hidden;
  font-size: 12px;
  line-height: 20px;
}
.chip-key {
  padding: 0 6px;
  background: #f1f5f9;
  color: var(--el-text-color-regular);
}
.chip-val {
  padding: 0 6px;
  font-weight: 600;
}
.cards {
  font-size: 12px;
  margin-left: 4px;
}
:deep(.clickable) {
  cursor: pointer;
}
.viewer {
  margin-top: 10px;
}
.tip {
  margin: 0;
  font-size: 11.5px;
  max-height: 320px;
  overflow: auto;
}
.config-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 10px;
}
.config-table {
  margin-bottom: 8px;
}
.block-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-top: 18px;
}
.block-head h3 {
  margin: 0;
}
.spacer {
  flex: 1;
}
.tiny {
  font-size: 11.5px;
}
.objective-line {
  margin: 0 0 10px;
  font-size: 12.5px;
}
.board-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.board-title {
  margin: 0 0 2px;
  font-size: 14px;
  font-weight: 600;
}
.board-title.screening {
  margin-top: 26px;
  color: var(--el-text-color-regular);
  font-weight: 500;
}
.pending-note {
  margin: -4px 0 10px;
}
.key {
  margin-left: 6px;
}
.sep {
  margin: 0 6px;
}
.stall-line {
  line-height: 1.6;
}
.not-planned {
  margin: -6px 0 14px;
}
.space-map {
  margin-bottom: 12px;
}
.breach {
  margin-left: 8px;
  font-size: 12px;
}
.better {
  color: var(--el-color-success);
  font-weight: 600;
}
.worse {
  color: var(--el-color-danger);
}
.baseline-tag {
  margin-right: 6px;
}
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 8px;
}
.warn-banner {
  margin-bottom: 12px;
}
.clock,
/* Flex with an explicit gap, not inline elements: Vue collapses the
   whitespace between them, which ran "ranked on" straight into the metric. */
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
.verdict-box {
  background: var(--autotune-bg);
  border: 1px solid var(--autotune-border);
  border-radius: 6px;
  padding: 12px 14px;
  margin-bottom: 18px;
}
.verdict-box .lead-in {
  min-width: 84px;
}
.headline {
  font-size: 16px;
}
.key-metrics {
  border: 1px solid var(--autotune-border);
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: 8px;
}
.metric-row {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 5px 10px;
  font-size: 12px;
  border-bottom: 1px solid var(--autotune-border);
}
.metric-row:last-child {
  border-bottom: none;
}
.metric-row .val {
  font-weight: 600;
}
.block.small {
  font-size: 11px;
  max-height: 260px;
  overflow: auto;
}
.preflight-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 7px 12px;
  background: #fff;
  border: 1px solid var(--autotune-border);
  border-radius: 6px;
}
.spacer {
  flex: 1;
}
.ok-text {
  color: var(--el-color-success);
}
.warn-text {
  color: var(--el-color-warning);
}
.fail-text {
  color: var(--el-color-danger);
}
.preflight-list {
  background: #fff;
  border: 1px solid var(--autotune-border);
  border-radius: 6px;
  padding: 4px 12px;
  margin-bottom: 12px;
}
.check {
  display: flex;
  gap: 10px;
  padding: 6px 0;
  font-size: 13px;
  line-height: 1.55;
  border-top: 1px solid var(--autotune-border);
}
.check:first-child {
  border-top: none;
}
.check .icon {
  width: 14px;
  flex: none;
  text-align: center;
  font-weight: 700;
}
.check.warn .icon {
  color: var(--el-color-warning);
}
.check.fail .icon {
  color: var(--el-color-danger);
}
.counts {
  display: flex;
  gap: 14px;
}
.passed {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 0;
  border-top: 1px solid var(--autotune-border);
}
.preflight-list > .passed:first-child {
  border-top: none;
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
.lead {
  margin: 0 0 14px;
  line-height: 1.6;
}
.block {
  background: #f1f5f9;
  padding: 12px;
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}
.log {
  max-height: 320px;
  overflow: auto;
}
.report {
  max-height: 60vh;
  overflow: auto;
  font-size: 12.5px;
  line-height: 1.5;
}
.promotions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  margin-bottom: 12px;
}
.promotion {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
</style>
