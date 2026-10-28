<script setup lang="ts">
import { apiErrorText } from '../utils/apiError'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  AlarmClock, Aim, ArrowDown, ArrowUp, CircleCheck, CircleCheckFilled, CircleCloseFilled, Coin,
  CopyDocument, Cpu, Delete, Document, Download, EditPen, MagicStick, MoreFilled, Odometer,
  RefreshRight, Tickets, Timer, Top, Bottom, Upload, VideoPause, VideoPlay, WarningFilled,
} from '@element-plus/icons-vue'
import { computed, h, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PluginSlot from '../components/PluginSlot.vue'
import {
  api,
  downloadText,
  fetchCampaignActivity,
  fetchPolicySessionLog,
  fetchRunLog,
  type Campaign,
  type CampaignSchedule,
  type Candidate,
  type LeaderboardEntry,
  type LogSource,
  type Machine,
  type MachineLifecycle,
  type MetricSpec,
  type Policy,
  type PolicySessionDetail,
  type Run,
  type RunDetail,
  type RunLaunch,
} from '../api/client'
import CatalogExportDialog, { type CatalogCandidate } from '../components/CatalogExportDialog.vue'
import InfoHint from '../components/InfoHint.vue'
import LogDialog from '../components/LogDialog.vue'
import NightlyWindow from '../components/NightlyWindow.vue'
import ConfigChips from '../components/ConfigChips.vue'
import CopyButton from '../components/CopyButton.vue'
import FactTile from '../components/FactTile.vue'
import PageLoading from '../components/PageLoading.vue'
import ScoreBars from '../components/ScoreBars.vue'
import SpaceMap from '../components/SpaceMap.vue'
import { useI18n } from '../i18n'
import { beatsBaselineRatio, fmtBaselineDelta } from '../utils/baseline'
import { campaignToYaml } from '../utils/campaignYaml'
import { copyText } from '../utils/clipboard'
import { all, useFirstLoad } from '../utils/load'
import { usePoll } from '../utils/poll'
import { sweptKeysOf, variedKeysOf, type SpaceShape } from '../utils/space'
import { campaignStatus, failureLabel, isLive, runLabel, runStatus } from '../utils/status'
import { absoluteTime, clockTime, duration, exactTime, relativeTime } from '../utils/time'
import { toYaml } from '../utils/yaml'
import { metricOptions, specFor } from '../utils/measures'

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
// The replay module's name is the deployment's (AUTOTUNE_LLMBENCH_REPLAY_MODULE),
// so the platform says which metric a second stage defaults to.
const defaultVerifyMetric = ref('replay.score_card_norm')
const parity = ref<{
  baseline_id: number | null
  missing: { flag: string; production: unknown }[]
} | null>(null)
const selectedRun = ref<RunDetail | null>(null)
const drawerOpen = ref(false)
const runLog = ref('')

/** The external policy container that searches for this campaign, when one
 *  does — the campaign row only carries its id. */
const policy = ref<Policy | null>(null)

async function load() {
  // At once, not one after another: on a slow link each request in a row
  // added a whole round trip, and this runs again on every poll.
  const [c, r, cands, ms, board, par, stages, sched] = await all(
    api.get(`/campaigns/${campaignId}`),
    api.get(`/campaigns/${campaignId}/runs`),
    api.get(`/campaigns/${campaignId}/candidates`),
    api.get('/machines'),
    api.get(`/campaigns/${campaignId}/leaderboard`),
    api.get(`/campaigns/${campaignId}/parity`),
    api.get('/machines/lifecycle'),
    api.get(`/campaigns/${campaignId}/schedule`),
  )
  campaign.value = c.data
  runs.value = r.data
  candidates.value = cands.data
  machines.value = ms.data
  leaderboard.value = board.data
  parity.value = par.data
  lifecycle.value = Object.fromEntries(
    (stages.data.machines as MachineLifecycle[]).map((m) => [m.machine_id, m]),
  )
  schedule.value = sched.data
  await Promise.all([loadPolicy(), loadSession()])
}

async function loadPolicy() {
  if (campaign.value?.policy_id == null || policy.value?.id === campaign.value.policy_id) return
  // The registry only lists; a deleted registration leaves the id showing.
  try {
    const listed = (await api.get('/policies')).data as Policy[]
    policy.value = listed.find((p) => p.id === campaign.value!.policy_id) ?? null
  } catch {
    policy.value = null
  }
}
const first = useFirstLoad(load)

// -- the policy's night: what the search container is doing ------------------
/** The latest policy session of a policy campaign, with what it is waiting on.
 *  Every session id, for the Logs menu: a session that never got as far as a
 *  run still has a container log worth reading. */
const sessionIds = ref<number[]>([])
const session = ref<PolicySessionDetail | null>(null)

async function loadSession() {
  if (campaign.value?.policy_id == null) return
  try {
    const rows = (await api.get(`/campaigns/${campaignId}/sessions`)).data as { id: number }[]
    sessionIds.value = rows.map((r) => r.id).sort((a, b) => b - a)
    session.value = sessionIds.value.length
      ? (await api.get(`/policy-sessions/${sessionIds.value[0]}`)).data
      : null
  } catch {
    session.value = null
  }
}

const SESSION_STEPS = ['starting', 'searching', 'finalizing', 'validating', 'done'] as const
const sessionEnded = computed(
  () => ['done', 'failed', 'aborted'].includes(session.value?.status ?? ''))
/** A policy still holding something on the cluster, whatever the campaign says. */
const policyLive = computed(() => !!session.value && !sessionEnded.value)
const sessionLook = computed(() => {
  const s = session.value
  if (!s) return { text: '', type: 'info' }
  if (s.status === 'failed') return { text: 'failed', type: 'danger' }
  if (s.status === 'aborted') return { text: 'aborted', type: 'info' }
  if (s.status === 'done') return { text: 'done', type: 'success' }
  if (s.abort_requested_at) return { text: 'aborting', type: 'warning' }
  if (s.finalize_requested_at && s.status === 'searching') return { text: 'stopping', type: 'warning' }
  return { text: s.status, type: s.status === 'starting' || s.status === 'pending' ? 'warning' : 'primary' }
})
/** Where it is on pending > starting > searching > finalizing > validating >
 *  done; a session that failed or was aborted stops where it was. */
function sessionStepClass(step: string, i: number) {
  const s = session.value
  if (!s) return ''
  const at = s.status === 'pending' ? 0 : SESSION_STEPS.indexOf(s.status as never)
  if (s.status === 'done' || (at >= 0 && i < at)) return 'done'
  if (i === at) return s.status === 'failed' ? 'bad' : 'now'
  return ''
}
/** Why a policy's search ended, in a few words rather than an identifier. */
const END_REASON: Record<string, string> = {
  policy_exhausted: 'nothing more to try',
  deadline: 'window ran out',
  explicit: 'stopped by request',
  policy_error: 'policy error',
  policy_idle: 'policy went idle',
  policy_wedged: 'policy stopped responding',
  container_died: 'container exited',
}

const sessionLine = computed(() => {
  const s = session.value
  if (!s) return ''
  const finalists = s.contenders.length
  switch (s.status) {
    case 'pending': return 'waiting for a machine'
    case 'starting':
      return s.started_at ? `for ${duration(s.started_at, new Date().toISOString())}` : ''
    case 'searching': {
      const parts = [`${s.trials.length} trial(s)`, `${finalists} finalist(s)`]
      if (s.finalize_requested_at) parts.push('wrapping up')
      return parts.join(' · ')
    }
    case 'finalizing': return 'winding down its engines'
    case 'validating': return `measuring ${finalists} finalist(s)`
    case 'done': return END_REASON[s.search_end_reason] ?? s.search_end_reason.replace(/_/g, ' ')
    case 'failed': return s.failure_class.replace(/_/g, ' ')
    case 'aborted': return ''
  }
  return ''
})

function openPolicyLog() {
  logOpen.value = true
}

/** The session's particulars, behind a toggle: most nights nobody needs them. */
const sessionDetails = ref(false)

async function abortSession() {
  if (!session.value) return
  try {
    await ElMessageBox.confirm(
      'Stop the policy now, without measuring its finalists?', 'Abort the search',
      { confirmButtonText: 'Abort', cancelButtonText: 'Cancel', type: 'warning' })
  } catch {
    return
  }
  try {
    await api.post(`/policy-sessions/${session.value.id}/abort`)
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, 'Abort failed'))
  }
}

/** A space written for this campaign alone lives only in it; saving it puts
 *  it on the Search spaces page for the next campaign. */
const spaceSaved = ref('')
async function saveSpace() {
  const c = campaign.value
  if (!c) return
  let name: string
  try {
    ({ value: name } = await ElMessageBox.prompt('A name for it on the Search spaces page.',
      'Save as search space', {
        inputValue: `${c.served_model_name}-${c.id}`, confirmButtonText: 'Save',
        cancelButtonText: 'Cancel', inputPattern: /\S/, inputErrorMessage: 'Give it a name',
      }))
  } catch {
    return
  }
  const space = (c.search_space ?? {}) as Record<string, unknown>
  try {
    await api.post('/search-spaces', {
      name: name.trim(), engine: c.engine, description: `From campaign ${c.name}`,
      base: space.base ?? {}, grid: space.grid ?? {}, tied: space.tied ?? [],
      ranges: space.ranges ?? {}, conditions: space.conditions ?? {},
    })
    spaceSaved.value = name.trim()
    ElMessage.success(`Saved as ${name.trim()}`)
  } catch (error) {
    ElMessage.error(apiErrorText(error, 'Could not save the search space'))
  }
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
  const spec = specFor(key, metricSpecs.value)
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
  // No second benchmark: the verify-stage runs are a policy's finalists,
  // measured again on the campaign's own benchmark and judged by its own
  // objective (as the platform does).
  campaign.value?.verify_benchmark_slug
    ? describe(campaign.value?.verify_objective, defaultVerifyMetric.value)
    : objective.value)

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

/** Production's config as the board measured it — the hover diffs each bar
 *  against it. The validation's flagged production run first, else the
 *  search's anchors. */
const productionConfig = computed<Record<string, unknown> | null>(() => {
  const base = verifiedBoard.value.find((e) => e.is_baseline) ?? screenBoard.value.find((e) => e.is_baseline)
  return base && !base.config?.__baseline__ ? base.config : null
})

/** The one config the board recommends: the best validated finalist that held
 *  its redlines, else the best such search result. */
const winnerEntry = computed<LeaderboardEntry | null>(() => {
  const pick = (rows: LeaderboardEntry[]) =>
    rows.find((e) => !e.is_baseline && e.holds_redlines && e.comparable !== false) ?? null
  return pick(verifiedBoard.value) ?? pick(screenBoard.value)
})
const winnerObjective = computed(() =>
  winnerEntry.value?.stage === 'verify' ? verifyObjective.value : objective.value)
/** What the winner changes from production — the keys worth reading. */
const winnerChanged = computed(() => {
  const w = winnerEntry.value
  if (!w) return []
  const prod = productionConfig.value ?? {}
  const keys = new Set([...Object.keys(w.config), ...Object.keys(prod)])
  return [...keys].filter((k) => !k.startsWith('__')
    && JSON.stringify(w.config[k]) !== JSON.stringify(prod[k])).sort()
})

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
    ElMessage.error(apiErrorText(error, 'Stop failed'))
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
    // The objective's names as the run was judged: a measure is not a key
    // of the raw metrics.
    named: (result.named ?? {}) as Record<string, number | null>,
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
    .map((key) => ({ key, value: key in v.named ? v.named[key] : v.metrics[key] }))
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
const catalogOpen = ref(false)

/** Every measured run on the boards, the deciding stage's entry first, for
 *  the model-catalog export. It opens with the baseline and the winner
 *  ticked. */
const catalogCandidates = computed<CatalogCandidate[]>(() => {
  const seen = new Set<number>()
  const out: CatalogCandidate[] = []
  for (const row of [...verifiedBoard.value, ...screenBoard.value]) {
    if (seen.has(row.run_id)) continue
    seen.add(row.run_id)
    out.push({
      run_id: row.run_id,
      label: row.is_baseline
        ? `${t('campaign.baselineRow')} · ${t('campaign.run')} ${row.run_id}`
        : `${t('campaign.run')} ${row.run_id}`,
      detail: row.score != null ? `${fmtScore(row.score)} (${row.stage})` : row.stage,
    })
  }
  return out
})
const catalogInitial = computed(() => {
  const board = verifiedBoard.value.length ? verifiedBoard.value : screenBoard.value
  const baseline = board.find((e) => e.is_baseline)
  const winner = board.find((e) => !e.is_baseline && e.holds_redlines)
  return [
    ...(baseline ? [{ run_id: baseline.run_id, role: 'baseline' }] : []),
    ...(winner ? [{ run_id: winner.run_id, role: 'optimized' }] : []),
  ]
})

/** This campaign as a file. Only the inputs — status, the window it happens to
 *  be serving and any force-start override are results of running it, and a
 *  file that carried them would recreate a campaign already half-finished. */
const router = useRouter()

/** A new campaign with this one's definition — never its results or window —
 *  filled into the New campaign form, to change before it is created. */
function copyToNew() {
  router.push({ path: '/campaigns/new', query: { from: String(campaignId) } })
}

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
    ElMessage.error(apiErrorText(error, 'Update failed'))
  }
}

/** Run outside the nightly window. Needs its own endpoint rather than just
 *  setting `active`: the schedule would see no open window on the next tick
 *  and put the campaign straight back to sleep. */
async function forceStart() {
  const scheduled = !!schedule.value?.daily_start
  try {
    await ElMessageBox.confirm(
      (scheduled ? 'Run this campaign now, outside its schedule? ' : 'Start this campaign now? ') +
        'It runs until its search is done or you stop it.',
      scheduled ? t('campaign.forceStart') : t('campaign.start'),
      { confirmButtonText: t('campaign.forceStart'), cancelButtonText: 'Cancel', type: 'info' },
    )
  } catch {
    return
  }
  try {
    await api.post(`/campaigns/${campaignId}/force-start`, {})
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, 'Could not start'))
    return
  }
  ElMessage.success('Running — the first run starts within a tick')
  await load()
}

const liveRuns = computed(() => runs.value.filter((r) => isLive(r.status)))

/** The Runs tab's rows. A policy trial is two runs, the engine the policy
 *  asked for and the benchmark of it (same candidate, same endpoint): the
 *  benchmark is nested under its engine, so each trial is one row. */
const runRows = computed<(Run & { children?: Run[] })[]>(() => {
  if (campaign.value?.policy_id == null) return runs.value
  const engines = new Map<string, Run & { children: Run[] }>()
  const rows: (Run & { children?: Run[] })[] = []
  for (const run of runs.value) {
    if (run.kind !== 'policy_launch') continue
    const row = { ...run, children: [] as Run[] }
    engines.set(`${run.candidate_id}|${run.endpoint_url}`, row)
  }
  for (const run of runs.value) {
    if (run.kind === 'policy_launch') {
      rows.push(engines.get(`${run.candidate_id}|${run.endpoint_url}`)!)
      continue
    }
    const engine = run.kind === 'external'
      ? engines.get(`${run.candidate_id}|${run.endpoint_url}`) : undefined
    if (engine) engine.children.push(run)
    else rows.push(run)
  }
  return rows
})

/** What a run of a policy campaign is for. */
function runRole(run: Run): string {
  if (run.kind === 'policy_launch') return 'trial'
  if (run.kind === 'external') return 'benchmark'
  if (run.stage === 'verify') return 'finalist'
  return 'search'
}
/** Stop, drawn as the square every player uses. */
const StopIcon = () =>
  h('svg', { viewBox: '0 0 1024 1024', fill: 'currentColor' },
    [h('rect', { x: 224, y: 224, width: 576, height: 576, rx: 64 })])

/** What the run control's Stop does now, the same test the server makes: a
 *  searching policy is first asked to wrap up (its finalists are measured),
 *  and only after that, or for anything else, does Stop end things at once.
 *  The button says which, since the two lose very different amounts. */
const stopIsRequest = computed(() => {
  const s = session.value
  return !!s && !!s.first_heartbeat_at && s.status === 'searching' && !s.finalize_requested_at
})

async function forceStop() {
  const live = liveRuns.value.length
  const windDown = stopIsRequest.value
  let message = 'Pause this campaign?'
  if (windDown) {
    message = 'Ask the policy to wrap up? Runs in progress finish, its finalists are measured, ' +
      'then it stops. Force stop stays available if it should end at once.'
  } else if (live || policyLive.value) {
    const what = [live ? `${live} run(s)` : '', policyLive.value ? 'the policy' : '']
      .filter(Boolean).join(' and ')
    message = `Stop now? ${what} will be ended; measurements in progress are lost.`
  }
  try {
    await ElMessageBox.confirm(message, windDown ? t('campaign.requestStop') : t('campaign.forceStop'), {
      confirmButtonText: windDown ? t('campaign.requestStop') : t('campaign.forceStop'),
      cancelButtonText: 'Cancel', type: windDown ? 'warning' : 'error',
      confirmButtonClass: windDown ? '' : 'el-button--danger',
    })
  } catch {
    return
  }
  try {
    await api.post(`/campaigns/${campaignId}/force-stop`)
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, 'Could not stop'))
    return
  }
  ElMessage.success(windDown ? 'Stop requested: the policy is wrapping up' : 'Stopped')
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
    ElMessage.error(apiErrorText(error, 'Could not run the checks'))
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
    ElMessage.success(draft.value.start && draft.value.end
      ? `Window saved: ${draft.value.start}–${draft.value.end} every night`
      : 'No window: the campaign runs when you start it')
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, 'Could not save the schedule'))
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
  // A finished campaign has nothing ahead of it: no window "awake now", no
  // override still "running".
  if (campaign.value?.status === 'done') {
    return s.daily_start && s.daily_end ? `${s.summary} — finished.` : 'Finished.'
  }
  const override = campaign.value?.override_until
  if (override && new Date(override) > new Date() && campaign.value?.status === 'active') {
    // Start (no window) and Run now (outside one) both run until the search is
    // done or someone stops it; the far-off cap behind that is not news.
    return s.daily_start
      ? `Started outside its window: runs until the search is done or you stop it, then back to ${s.summary}.`
      : 'Running until the search is done or you stop it.'
  }
  if (!s.daily_start || !s.daily_end) {
    return campaign.value?.status === 'active'
      ? 'No window — running until the search is done or you stop it.'
      : 'No window — started and stopped by hand.'
  }
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

// -- the summary tiles: one fact each, the rest in their tooltips -------------

const scheduleFact = computed(() => {
  const c = campaign.value
  const s = schedule.value
  if (!c) return { value: '', sub: '' }
  const windowed = !!(c.daily_start && c.daily_end)
  const value = windowed ? `${c.daily_start}–${c.daily_end}` : t('campaign.fact.manual')
  const override = !!c.override_until && new Date(c.override_until) > new Date()
    && c.status === 'active'
  let sub = ''
  if (c.status === 'done') sub = t('campaign.fact.finished')
  else if (override || (!windowed && c.status === 'active')) sub = t('campaign.fact.runningNow')
  else if (!windowed) sub = t('campaign.fact.byHand')
  else if (s?.finished) sub = t('campaign.fact.noNightsLeft')
  else if (s?.current_window) sub = t('campaign.fact.awakeUntil', { time: clockTime(s.current_window.end) })
  else if (s?.next_window) sub = t('campaign.fact.next', { when: relativeTime(s.next_window.start) })
  return { value, sub }
})

const searchFact = computed(() => {
  const c = campaign.value
  if (!c) return { value: '', sub: '', tip: '' }
  const size = spaceSize.value
  return {
    value: c.policy_id != null
      ? (policy.value?.name ?? `#${c.policy_id}`)
      : t('campaign.fact.everyConfig'),
    sub: [spaceName.value, size ? t('campaign.fact.configs', { n: size }) : '']
      .filter(Boolean).join(' · '),
    tip: [
      c.policy_id != null ? (policy.value?.image ?? '') : t('campaign.enumerates'),
      sweptKeys.value.length ? `${t('campaign.fact.swept')}: ${sweptKeys.value.join(', ')}` : '',
    ].filter(Boolean).join('\n'),
  }
})

const progressFact = computed(() => {
  const { done, total } = progress.value
  const failed = new Set(runs.value.filter((r) => r.status === 'failed').map((r) => r.candidate_id))
  return {
    percent: total ? Math.min(100, Math.round((done / total) * 100)) : 0,
    text: total ? `${done} / ${total}` : `${done}`,
    sub: [
      liveRuns.value.length ? t('campaign.fact.running', { n: liveRuns.value.length }) : '',
      failed.size ? t('campaign.fact.failed', { n: failed.size }) : '',
    ].filter(Boolean).join(' · '),
  }
})

/** A leaderboard row opens the run that produced its score. */
function openBoardRow(row: { run_id?: number | null }) {
  if (row.run_id) void openRun({ id: row.run_id } as Run)
}

/** Bumped per click: on a slow network a second row clicked before the first
 *  one's answer arrived must not have its drawer replaced by the first. */
let openSeq = 0
async function openRun(run: Run) {
  const seq = ++openSeq
  const detail = (await api.get(`/runs/${run.id}`)).data
  // A benchmark of a policy's launch owns no engine: its launch (the applied
  // command, the pod) is the engine run it measured.
  const launchOf = detail.launch_run_id ?? detail.id
  const [log, launch] = await Promise.all([
    fetchRunLog(run.id),
    api.get(`/agent/v1/runs/${launchOf}/launch`).then((r) => r.data as RunLaunch).catch(() => null),
  ])
  if (seq !== openSeq) return
  selectedRun.value = detail
  runLog.value = log
  runLaunch.value = launch
  launchTab.value = 'serve'
  drawerOpen.value = true
}

/** The launch tabs' contents, each one copyable as-is. */
const runLaunch = ref<RunLaunch | null>(null)
const launchTab = ref('serve')
const launchYaml = computed(() => {
  const l = runLaunch.value
  if (!l) return ''
  const c = l.config
  return toYaml({
    engine: c.engine, image: c.image, model_path: c.model_path,
    served_model_name: c.served_model_name, gpu_type: c.gpu_type || undefined, cards: l.cards,
    engine_args: c.engine_args,
    ...(Object.keys(c.extra_env ?? {}).length ? { extra_env: c.extra_env } : {}),
    ...(Object.keys(c.extra_volumes ?? {}).length ? { extra_volumes: c.extra_volumes } : {}),
  })
})
/** What the platform actually applied — the launch's own record when the run
 *  was a benchmark of it. */
const appliedCommand = computed(() =>
  runLaunch.value?.rendered.docker_command || selectedRun.value?.launch_command || '')
const engineArgsJson = computed(() =>
  JSON.stringify(runLaunch.value?.config.engine_args ?? selectedRun.value?.config ?? {}, null, 2))

function downloadRunLog() {
  const r = selectedRun.value
  if (r) downloadText(`${campaign.value?.name ?? 'campaign'}-run-${r.id}.log`, runLog.value)
}

// -- the Logs submenu: the policy container + every engine run, in one place --
const logOpen = ref(false)

const logSources = computed<LogSource[]>(() => {
  const name = campaign.value?.name ?? 'campaign'
  // What the platform did, from its own record: there even when a container
  // printed nothing or never started.
  const sources: LogSource[] = [{
    key: 'activity',
    label: t('campaign.activityLog'),
    fetch: () => fetchCampaignActivity(campaignId),
    filename: `${name}-activity.log`,
  }]
  // Each policy session's container log (policy campaigns), newest first.
  for (const sid of sessionIds.value) {
    sources.push({
      key: `policy-${sid}`,
      label: t('campaign.policyLog', { id: sid }),
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
      label: t('campaign.engineLog', { id: r.id, status: runLabel(r.status) }),
      fetch: () => fetchRunLog(r.id),
      filename: `${name}-run-${r.id}.log`,
    })
  }
  return sources
})

/** Every setting the campaign was created with, in one place — so a night can
 *  be reproduced, compared against another campaign, or have one field lifted
 *  out of it without reading the database. */
/** "≈ 6 min startup + 22 min benchmark", or a note that nothing ran yet. */
function moreAction(command: string) {
  if (command === 'logs') logOpen.value = true
  else if (command === 'clone') copyToNew()
  else if (command === 'export') exportOpen.value = true
  else if (command === 'retry') void retryFailed()
  else if (command === 'delete') void deleteCampaign()
  else if (command === 'done') void markDone()
}

/** Closing a stopped campaign by hand, so it stops holding its machines. */
const closable = computed(() =>
  !deletable.value && ['paused', 'scheduled'].includes(campaign.value?.status ?? ''))

async function markDone() {
  const name = campaign.value?.name ?? ''
  try {
    await ElMessageBox.confirm(t('campaign.markDoneConfirm', { name }), t('campaign.markDone'),
      { confirmButtonText: t('campaign.markDone'), cancelButtonText: 'Cancel', type: 'warning' })
  } catch {
    return
  }
  try {
    await api.put(`/campaigns/${campaignId}/status`, { status: 'done' })
    ElMessage.success(t('campaign.markedDone', { name }))
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('campaign.markDoneFailed')))
  }
}

/** Only a campaign that never ran can go: one with runs keeps its results. */
const deletable = computed(() => runs.value.length === 0 && sessionIds.value.length === 0)

async function deleteCampaign() {
  const name = campaign.value?.name ?? ''
  try {
    await ElMessageBox.confirm(t('campaign.deleteConfirm', { name }), t('campaign.deleteCampaign'),
      { confirmButtonText: t('campaign.deleteCampaign'), cancelButtonText: 'Cancel',
        type: 'warning', confirmButtonClass: 'el-button--danger' })
  } catch {
    return
  }
  try {
    await api.delete(`/campaigns/${campaignId}`)
    ElMessage.success(t('campaign.deleted', { name }))
    router.push('/campaigns')
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('campaign.deleteFailed')))
  }
}

function learnedRunLength(c: Campaign): string {
  const t = c.learned_timing
  if (t?.bench_minutes == null && t?.estimated_bench_minutes != null) {
    return `≈ ${Math.round(t.estimated_bench_minutes)} min benchmark, estimated from its settings`
  }
  if (!t || t.bench_minutes == null) return 'not measured yet'
  const parts = [
    t.startup_minutes != null ? `${Math.round(t.startup_minutes)} min startup` : '',
    `${Math.round(t.bench_minutes)} min benchmark`,
  ].filter(Boolean)
  return `≈ ${parts.join(' + ')}`
}

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
    ...(hasRecipe.value
      ? [{
          label: 'Chart',
          value: `${c.recipe?.chart?.name ?? c.engine} ` +
            (c.chart_version || `${c.recipe?.chart?.version || 'latest'} — not pinned yet`),
          hint: c.chart_version
            ? 'pinned: every run renders this version'
            : 'pinned to the version the first run installs',
        }]
      : []),
    ...(c.recipe_source
      ? [{ label: 'Recipe from', value: c.recipe_source, hint: 'the catalog variant it started as' }]
      : []),
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
    ...(!c.dataset_profile && c.dataset_frozen
      ? [{
          label: 'Replay dataset',
          value: `${c.dataset_frozen} (frozen)`,
          hint: 'a file on LLMBench that never changes; nothing to pin',
        }]
      : []),
    ...(c.dataset_profile
      ? [{
          label: 'Replay dataset',
          value: c.dataset_build_id
            ? `${c.dataset_profile} @ ${c.dataset_build_id}`
            : `${c.dataset_profile} — not pinned yet`,
          hint: c.dataset_build_id
            ? `sample ${c.dataset_policy_applied}; kept for this campaign's whole life`
            : c.dataset_policy === 'use_current'
              ? 'uses the current sample when it starts'
              : 'takes a fresh sample when it starts',
        }]
      : []),
    { label: 'Machines', value: (c.machine_names ?? []).join(', ') || 'any registered machine' },
    // Who decides what to try: an external policy container, or — with none —
    // every configuration in the space, in declaration order.
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
      : [{
          label: 'Search',
          value: 'every configuration in the space, in order',
        }]),
    {
      label: 'Run length',
      value: learnedRunLength(c),
      hint: `learned from this campaign's runs; capped at ${c.max_run_minutes} min`,
    },
    Object.keys(c.recipe ?? {}).length
      ? {
          label: 'Production reference',
          value: 'the recipe as written',
          hint: 'runs of the unchanged recipe are the baseline every change is compared to',
        }
      : {
          label: 'Production reference',
          value: c.run_baseline_canary ? 'measured' : 'not measured',
          hint: "the recorded baseline config, launched and benchmarked like a candidate",
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
const hasRecipe = computed(() => Object.keys(campaign.value?.recipe ?? {}).length > 0)
const recipeYaml = computed(() => toYaml(campaign.value?.recipe ?? {}))
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
const sweptKeys = computed<string[]>(() => {
  // The declared axes first, then whatever else the runs actually changed: a
  // policy's best configs may differ only outside the declared space.
  const declared = sweptKeysOf(space.value)
  const varied = variedKeysOf(candidates.value.map((c) => (c.config ?? {}) as Record<string, unknown>))
  return [...declared, ...varied.filter((k) => !declared.includes(k))]
})

const baseConfig = computed<Record<string, unknown>>(() => space.value.base ?? {})

/** What became of a candidate, in the words of what actually happened to it:
 *  the run is authoritative, the candidate's own status only says whether the
 *  platform still owes it a run. */
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
 *  Not the number of candidate rows: the space is expanded only once
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

const progress = computed(() => {
  // The denominator is the LARGER of the declared space size and the candidate
  // rows created so far — not the row count alone. Enumeration materializes
  // candidates in batches (max_new per pass), so mid-search there are fewer rows
  // than the space will produce, and dividing by them read a 256-config sweep as
  // "101/205" — a moving denominator still being planned. Verify repeats, on the
  // other hand, push the real total PAST the declared size (48 screening + 3
  // replays is 51 runs), so once rows exceed it they win. The max satisfies both:
  // the declared size is a floor while planning, the row count takes over once
  // repeats grow past it. Before planning both fall back and it reads 0/256.
  const total = Math.max(candidates.value.length, spaceSize.value ?? 0) || spaceSize.value
  // Candidates, not runs, on top too: one candidate can be several runs — a
  // policy's launch plus the benchmark of it, or a retry — and counting runs
  // read 8 policy configs as "18/10". A candidate is done once it has a
  // finished run and none still going.
  const live = new Set(runs.value.filter((r) => isLive(r.status)).map((r) => r.candidate_id))
  const finished = new Set(
    runs.value.filter((r) => !isLive(r.status) && !live.has(r.candidate_id)).map((r) => r.candidate_id),
  )
  return { total, done: finished.size }
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
  // A config queued again (a campaign Stop gives its cut-off runs back) is
  // not one to re-run: it will run on the next start anyway.
  const queued = new Set(candidates.value.filter((c) => c.status === 'valid').map((c) => c.id))
  let n = 0
  for (const [id, statuses] of byCandidate) {
    if (queued.has(id)) continue
    if (statuses.length && statuses.every((st) => st === 'failed' || st === 'killed')) n += 1
  }
  return n
})

const poll = usePoll(load, 10000) // polling for PoC; SSE later
onMounted(() => {
  void first.run()
  // Linked from the Runs page: open that run's details.
  const linked = Number(route.query.run)
  if (linked) void openRun({ id: linked } as Run)
  api.get('/objectives/metrics').then(({ data }) => {
    metricSpecs.value = metricOptions(data)
    if (data.default_verify_target_metric) defaultVerifyMetric.value = data.default_verify_target_metric
  })
  poll.start()
})
</script>

<template>
  <div class="page" v-if="campaign">
    <div class="header-row">
      <div class="title-block">
        <h1 class="page-title">{{ campaign.name }}</h1>
        <el-tag :type="campaignStatus(campaign.status).type" effect="light"
          :class="['status-tag', campaignStatus(campaign.status).cls]">
          <span v-if="campaign.status === 'active'" class="live-dot" />{{ campaign.status }}
        </el-tag>
        <span class="muted tiny owner">
          <template v-if="campaign.owner_name">{{ campaign.owner_name }} · </template>
          <span :title="exactTime(campaign.created_at)">{{ relativeTime(campaign.created_at) }}</span>
        </span>
        <!-- A plugin runs this campaign for something of its own: say so, and
             link back to where it is managed. -->
        <span v-if="campaign.managed_by" class="muted tiny owner">
          ·
          <router-link v-if="campaign.managed_by.link" :to="campaign.managed_by.link" class="nav-link">
            {{ t('campaigns.runBy', { label: campaign.managed_by.label || campaign.managed_by.plugin }) }}
          </router-link>
          <template v-else>
            {{ t('campaigns.runBy', { label: campaign.managed_by.label || campaign.managed_by.plugin }) }}
          </template>
        </span>
      </div>
      <div class="actions">
        <el-button :icon="Tickets" @click="logOpen = true">{{ t('campaign.logs') }}</el-button>
        <el-button :icon="Document" @click="openReport">{{ t('campaign.report') }}</el-button>
        <el-button :icon="Upload" :disabled="!catalogCandidates.length" @click="catalogOpen = true">
          {{ t('catalogExport.menu') }}
        </el-button>
        <!-- The occasional actions, one click away instead of a row of buttons
             nobody reads past. -->
        <el-dropdown trigger="click" @command="moreAction">
          <el-button :icon="MoreFilled" :aria-label="t('campaign.more')" />
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="clone" :icon="CopyDocument">
                {{ t('campaign.copyToNew') }}
              </el-dropdown-item>
              <el-dropdown-item command="export" :icon="Download"
                title="Everything needed to recreate this campaign — import it on the New campaign page">
                {{ t('campaign.exportYaml') }}
              </el-dropdown-item>
              <!-- Re-queues only candidates whose every run failed, then puts
                   the campaign back to work. -->
              <el-dropdown-item v-if="retryableCount" command="retry" :icon="RefreshRight">
                {{ retryableCount === 1
                  ? t('campaign.rerunFailedOne', { n: retryableCount })
                  : t('campaign.rerunFailedMany', { n: retryableCount }) }}
              </el-dropdown-item>
              <el-dropdown-item v-if="closable" command="done" :icon="CircleCheck" divided>
                {{ t('campaign.markDone') }}
              </el-dropdown-item>
              <el-dropdown-item v-if="deletable" command="delete" :icon="Delete" divided>
                {{ t('campaign.deleteCampaign') }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>

        <!-- The run control: one segmented group, the way forward first and
             Stop always last. Pause lets current runs finish; Stop ends them.
             `done` offers neither: the search is exhausted, and the honest
             actions are the report and re-running what failed. -->
        <el-button-group v-if="campaign.status !== 'done'" class="run-control">
          <template v-if="campaign.status === 'active'">
            <el-button :icon="VideoPause" title="No new runs; the ones going now finish"
              @click="setStatus('paused')">{{ t('campaign.pause') }}</el-button>
          </template>
          <template v-else-if="campaign.status === 'scheduled'">
            <el-button type="primary" :icon="VideoPlay" @click="forceStart">
              {{ t('campaign.forceStart') }}</el-button>
            <el-button :icon="VideoPause" title="Skip the coming windows until resumed"
              @click="setStatus('paused')">{{ t('campaign.pause') }}</el-button>
          </template>
          <template v-else-if="campaign.status === 'paused' && schedule?.daily_start">
            <el-button type="primary" :icon="VideoPlay" @click="forceStart">
              {{ t('campaign.forceStart') }}</el-button>
            <el-button :icon="AlarmClock" @click="setStatus('scheduled')">
              {{ t('campaign.resumeSchedule') }}</el-button>
          </template>
          <!-- A policy campaign with no window cannot simply be activated (its
               session deadlines come from the window): Run now writes one. -->
          <el-button v-else-if="campaign.policy_id != null && !campaign.window_end"
            type="primary" :icon="VideoPlay" @click="forceStart">
            {{ t('campaign.start') }}</el-button>
          <el-button v-else type="primary" :icon="VideoPlay" @click="setStatus('active')">
            {{ t('campaign.start') }}</el-button>
          <el-button v-if="campaign.status === 'active' || liveRuns.length || policyLive"
            :class="stopIsRequest ? 'stop-request' : 'stop'" :icon="StopIcon"
            :title="stopIsRequest
              ? 'Ask the policy to wrap up: runs in progress finish and its finalists are measured'
              : 'Stop now: runs in progress are ended and their measurements lost'"
            @click="forceStop">{{ stopIsRequest ? t('campaign.requestStop') : t('campaign.forceStop') }}</el-button>
        </el-button-group>
      </div>
    </div>

    <!-- The campaign in one row: each tile is one fact, with the detail one
         hover away and the whole definition on the Configuration tab. -->
    <div class="facts">
      <FactTile :icon="Cpu" :label="t('campaign.fact.model')">
        {{ campaign.served_model_name }}
        <template #sub><span class="mono">{{ campaign.engine }}</span></template>
        <template #tip>{{ campaign.image }}
{{ campaign.model_path }}</template>
      </FactTile>
      <FactTile :icon="MagicStick" :label="t('campaign.fact.search')">
        {{ searchFact.value }}
        <template v-if="searchFact.sub" #sub>{{ searchFact.sub }}</template>
        <template v-if="searchFact.tip" #tip>{{ searchFact.tip }}</template>
      </FactTile>
      <FactTile :icon="Aim" :label="t('campaign.fact.objective')">
        <el-icon class="dir"><Bottom v-if="objective.direction === 'minimize'" /><Top v-else /></el-icon>
        {{ objective.name || objective.label }}
        <template #sub>{{ objective.redlines.length
          ? t('campaign.fact.redlines', { n: objective.redlines.length })
          : t('campaign.fact.noRedlines') }}</template>
        <template #tip>{{ objective.direction }} {{ objective.label }} ({{ objective.key }})<template
          v-for="(r, i) in objective.redlines" :key="i">
{{ r.metric }} {{ r.op }} {{ r.value }}</template></template>
      </FactTile>
      <FactTile :icon="Timer" :label="t('campaign.fact.schedule')">
        {{ scheduleFact.value }}
        <template #action>
          <el-button link type="primary" size="small" :icon="EditPen"
            :aria-label="schedule?.daily_start ? t('campaign.editSchedule') : t('campaign.addSchedule')"
            :title="schedule?.daily_start ? t('campaign.editSchedule') : t('campaign.addSchedule')"
            @click="openScheduleEditor" />
        </template>
        <template v-if="scheduleFact.sub" #sub>{{ scheduleFact.sub }}</template>
        <template #tip>{{ scheduleLine }}<template v-if="campaign.schedule_timezone">
{{ campaign.schedule_timezone }}</template></template>
      </FactTile>
      <FactTile :icon="Odometer" :label="t('campaign.fact.progress')">
        <div class="progress">
          <el-progress :percentage="progressFact.percent" :show-text="false" :stroke-width="6"
            :status="campaign.status === 'done' ? 'success' : undefined" />
          <span>{{ progressFact.text }}</span>
        </div>
        <template v-if="progressFact.sub" #sub>{{ progressFact.sub }}</template>
        <template #tip>{{ t('campaign.evaluated', { done: progress.done, total: progress.total }) }}</template>
      </FactTile>
      <FactTile v-if="campaign.dataset_profile" :icon="Coin" :label="t('campaign.fact.dataset')">
        {{ campaign.dataset_profile }}
        <template #sub><span :class="{ mono: !!campaign.dataset_build_id }">{{
          campaign.dataset_build_id || t('campaign.fact.notPinned') }}</span></template>
        <template #tip>{{ t('campaign.fact.datasetTip') }}</template>
      </FactTile>
      <FactTile v-else-if="campaign.dataset_frozen" :icon="Coin" :label="t('campaign.fact.dataset')">
        {{ campaign.dataset_frozen }}
        <template #sub>{{ t('campaign.fact.frozen') }}</template>
        <template #tip>{{ t('campaign.fact.frozenTip') }}</template>
      </FactTile>
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

    <el-alert v-if="stall" type="warning" :closable="false" :title="stall.title"
      class="warn-banner" show-icon>
      <template #default>
        <div v-for="(line, i) in stall.lines" :key="i" class="stall-line">{{ line }}</div>
      </template>
    </el-alert>

    <!-- The policy's night in one line: where its container is on the way
         through, and why it ended. The particulars are behind Details. -->
    <div v-if="session" class="session-strip">
      <div class="session-line">
        <el-icon class="strip-icon"><MagicStick /></el-icon>
        <b>{{ policy?.name ?? `#${campaign.policy_id}` }}</b>
        <span class="session-track">
          <span v-for="(step, i) in SESSION_STEPS" :key="step" class="session-step"
            :class="sessionStepClass(step, i)" :title="step">
            <span class="pip" /><span class="step-name">{{ step }}</span>
          </span>
        </span>
        <!-- The track already says where it is; the tag only for what it cannot
             (failed, aborted, a stop on the way). -->
        <el-tag v-if="!(SESSION_STEPS as readonly string[]).includes(sessionLook.text)"
          size="small" :type="(sessionLook.type as any)">{{ sessionLook.text }}</el-tag>
        <span v-if="sessionLine" class="muted tiny">{{ sessionLine }}</span>
        <span class="spacer" />
        <el-button size="small" text @click="sessionDetails = !sessionDetails">
          {{ t('campaign.details') }}
          <el-icon class="el-icon--right"><ArrowUp v-if="sessionDetails" /><ArrowDown v-else /></el-icon>
        </el-button>
        <el-button size="small" text @click="openPolicyLog">{{ t('campaign.logs') }}</el-button>
        <el-button v-if="!sessionEnded" size="small" text class="risky" @click="abortSession">
          {{ t('campaign.abort') }}
        </el-button>
      </div>
      <div v-if="session.waiting" class="session-warn tiny">
        <el-icon><WarningFilled /></el-icon> {{ session.waiting }}</div>
      <div v-if="session.error" class="session-bad tiny">
        <el-icon><CircleCloseFilled /></el-icon> {{ session.error }}</div>
      <el-collapse-transition>
        <dl v-if="sessionDetails" class="session-details">
          <dt>{{ t('campaign.session') }}</dt><dd class="mono">{{ session.id }}</dd>
          <dt>{{ t('common.started') }}</dt>
          <dd :title="exactTime(session.started_at)">{{ session.started_at ? absoluteTime(session.started_at) : '—' }}</dd>
          <dt>{{ t('campaign.lastHeard') }}</dt>
          <dd :title="exactTime(session.last_heartbeat_at)">{{ relativeTime(session.last_heartbeat_at) }}</dd>
          <template v-if="session.search_deadline">
            <dt>{{ t('campaign.searchUntil') }}</dt><dd>{{ absoluteTime(session.search_deadline) }}</dd>
          </template>
          <dt>{{ t('campaign.trials') }}</dt><dd>{{ session.trials.length }}</dd>
          <dt>{{ t('campaign.finalistsShort') }}</dt><dd>{{ session.contenders.length }}</dd>
          <template v-if="session.policy_message">
            <dt>{{ t('campaign.policySays') }}</dt><dd>{{ session.policy_message }}</dd>
          </template>
          <template v-if="session.search_end_reason">
            <dt>{{ t('campaign.endedBecause') }}</dt><dd class="mono">{{ session.search_end_reason }}</dd>
          </template>
        </dl>
      </el-collapse-transition>
    </div>

    <!-- Every pre-run finding in one place, run on demand rather than as a
         banner, and only while a start is still ahead: on a running campaign
         the port check reports our OWN engine holding the port, and on a
         finished one every answer is about a machine it no longer uses. -->
    <div v-if="preflightUseful" class="preflight-bar">
      <b class="preflight-label">{{ t('campaign.preflight') }}</b>
      <span v-if="!preflight" class="muted">{{ t('campaign.preflightNotRun') }}</span>
      <span v-else class="counts">
        <span v-if="preflightCounts.fail" class="fail-text">
          <el-icon><CircleCloseFilled /></el-icon>
          {{ t('campaign.preflightBlocking', { n: preflightCounts.fail }) }}</span>
        <span v-if="preflightCounts.warn" class="warn-text">
          <el-icon><WarningFilled /></el-icon>
          {{ t('campaign.preflightWarnings', { n: preflightCounts.warn }) }}</span>
        <el-tooltip :disabled="!preflightPassed.length" placement="bottom">
          <template #content>
            <div v-for="(c, i) in preflightPassed" :key="i">{{ c.machine }}: {{ c.label }}</div>
          </template>
          <span class="ok-text"><el-icon><CircleCheckFilled /></el-icon>
            {{ t('campaign.preflightPassed', { n: preflightCounts.pass }) }}</span>
        </el-tooltip>
      </span>
      <span class="spacer" />
      <el-button size="small" :loading="checking" @click="runPreflight">
        {{ preflight ? t('campaign.recheck') : t('campaign.runPreflight') }}
      </el-button>
    </div>

    <div v-if="preflight && preflightIssues.length" class="preflight-list">
      <div v-for="(c, i) in preflightIssues" :key="i" class="check" :class="c.status">
        <el-icon class="icon"><CircleCloseFilled v-if="c.status === 'fail'" /><WarningFilled v-else /></el-icon>
        <div>
          <span class="mono muted">{{ c.machine }}</span>
          <b> {{ c.label }}</b> — {{ c.detail }}
          <InfoHint v-if="c.hint">{{ c.hint }}</InfoHint>
        </div>
      </div>
    </div>

    <!-- Sections installed plugins add about this campaign (src/plugins). -->
    <PluginSlot name="campaign-detail.sections" :props="{ campaign, reload: load }" />

    <el-tabs>
      <el-tab-pane :label="t('campaign.tabs.leaderboard')">
        <!-- The answer first: the one config the board says to run, with what
             it changes from production and its commands a click away. The
             bars below are the evidence; hover one for its config. -->
        <div v-if="winnerEntry" class="winner-card">
          <div class="winner-head">
            <span class="winner-badge">{{ t('campaign.winner') }}</span>
            <b class="winner-score mono">{{ fmtScore(winnerEntry.score) }}</b>
            <span class="muted tiny">{{ winnerObjective.unit || winnerObjective.label }}</span>
            <span v-if="winnerEntry.vs_baseline != null" class="mono"
              :class="beatsBaseline(winnerEntry.vs_baseline, winnerObjective.direction) ? 'better' : 'worse'">
              {{ fmtVsBaseline(winnerEntry.vs_baseline, winnerObjective.direction) }} {{ t('campaign.vsBaseline') }}</span>
            <el-tag size="small" effect="plain" :type="winnerEntry.stage === 'verify' ? 'warning' : 'info'">
              {{ winnerEntry.stage === 'verify' ? t('campaign.validated') : t('campaign.screenOnly') }}</el-tag>
            <el-tag v-if="winnerEntry.card_type" size="small" effect="plain" type="info">{{ winnerEntry.card_type }}</el-tag>
            <span class="muted tiny">{{ t('campaign.run') }} #{{ winnerEntry.run_id }}</span>
            <span class="winner-actions">
              <el-button size="small" type="primary" plain @click="openBoardRow(winnerEntry)">
                {{ t('campaign.launchConfig') }}</el-button>
            </span>
          </div>
          <div class="winner-config">
            <span class="muted tiny">{{ t('campaign.changesFromBaseline') }}</span>
            <ConfigChips v-if="winnerChanged.length" :config="winnerEntry.config" :keys="winnerChanged" />
            <span v-else class="muted">{{ t('campaign.sameAsBaseline') }}</span>
          </div>
        </div>
        <!-- The expensive stage first, in its own table: it decides the winner,
             and its scores come from a different benchmark in different units.
             One table sorted across both would be a ranking of nothing. -->
        <template v-if="verifiedBoard.length">
          <div class="board-head">
            <h3 class="board-title">
              {{ staged ? t('campaign.verifiedOn') : t('campaign.finalists') }}
              <InfoHint v-if="!staged" :width="340">{{ t('campaign.finalistsHint') }}</InfoHint>
            </h3>
            <span class="chip-plain mono" :title="t('campaign.benchmark')">
              {{ campaign?.verify_benchmark_slug || campaign?.benchmark_slug }}</span>
            <el-tooltip :content="`${verifyObjective.direction} ${verifyObjective.key}`" placement="top">
              <span class="chip-plain">
                <el-icon><Bottom v-if="verifyObjective.direction === 'minimize'" /><Top v-else /></el-icon>
                {{ verifyObjective.label }}</span>
            </el-tooltip>
            <el-tooltip v-if="datasetPin" placement="top" :width="320">
              <template #content>{{ t('campaign.replayedTip') }}</template>
              <span class="chip-plain">
                <el-icon><Coin /></el-icon>
                <span class="mono">{{ datasetPin.build }}</span>
                <template v-if="datasetPin.adopted"> · {{ t('campaign.datasetAdopted') }}</template>
              </span>
            </el-tooltip>
            <InfoHint v-if="staged">
              Production was measured with the screening benchmark, not this one, so there
              is no like-for-like comparison against it here.
            </InfoHint>
          </div>
          <ScoreBars :rows="verifiedBoard" :swept-keys="sweptKeys"
            :direction="verifyObjective.direction" :production="productionConfig"
            :unit="verifyObjective.unit" @select="openBoardRow" />
        </template>

        <div class="board-head" :class="{ screening: verifiedBoard.length }">
          <h3 v-if="verifiedBoard.length" class="board-title">{{ t('campaign.screening') }}</h3>
          <el-tooltip :content="`${objective.direction} ${objective.key}`" placement="top">
            <span class="chip-plain">
              <el-icon><Bottom v-if="objective.direction === 'minimize'" /><Top v-else /></el-icon>
              {{ objective.label }}</span>
          </el-tooltip>
          <span v-for="(r, i) in objective.redlines" :key="i" class="chip-plain mono redline">
            {{ r.metric }} {{ r.op }} {{ r.value }}</span>
          <InfoHint v-if="objective.redlines.length">
            Redlines. A config that crosses one is rejected however fast it was — and a
            metric the benchmark never reported counts as crossed, because we cannot
            certify an SLO we did not measure.
          </InfoHint>
          <span v-if="staged && !verifiedBoard.length" class="muted tiny">
            {{ t('campaign.thenVerify', { n: campaign?.verify_top_k ?? 0 }) }}
            <span class="mono">{{ campaign?.verify_benchmark_slug }}</span>
          </span>
        </div>
        <ScoreBars :rows="screenBoard" :swept-keys="sweptKeys"
          :direction="objective.direction" :production="productionConfig"
          :unit="objective.unit" @select="openBoardRow" />
        <el-empty v-if="screenBoard.length === 0 && !verifiedBoard.length" :image-size="64"
          :description="t('campaign.noRuns')" />
      </el-tab-pane>

      <el-tab-pane :label="t('campaign.tabs.runs')">
        <el-table :data="runRows" row-key="id" @row-click="openRun" style="cursor: pointer">
          <!-- Wide enough for a nested benchmark row's indent. -->
          <el-table-column prop="id" label="ID" width="96" />
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
          <!-- A policy campaign runs two things per trial: the engine the
               policy asked for (held while it serves) and the benchmark of
               it, nested under the engine (runRows). -->
          <el-table-column v-if="campaign.policy_id != null" label="Role" width="120">
            <template #default="{ row }">
              <span class="muted tiny">{{ runRole(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="runs.some((r) => r.failure_class)" label="Failure" width="130">
            <template #default="{ row }">{{ failureLabel(row.failure_class || '') }}</template>
          </el-table-column>
          <!-- Which cards, by number: "0" read as "no GPUs". -->
          <el-table-column label="Cards" width="110">
            <template #default="{ row }">
              <span class="mono">{{ row.gpu_indices?.length
                ? row.gpu_indices.map((i: number) => `#${i}`).join(' ') : '—' }}</span>
            </template>
          </el-table-column>
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
            Snapshot at creation
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
              <div>{{ row.label }} <InfoHint v-if="row.hint">{{ row.hint }}</InfoHint></div>
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

        <template v-if="hasRecipe">
          <div class="block-head">
            <h3>Recipe</h3>
            <InfoHint>The engine chart's values every run starts from, as a model-catalog
              variant writes them. A run is this with its configuration applied, and a
              winner is published as the recipe it ran.</InfoHint>
            <span class="spacer" />
            <el-button size="small" text type="primary"
              @click="copyText(recipeYaml, 'Recipe copied')">Copy YAML</el-button>
          </div>
          <pre class="mono block">{{ recipeYaml }}</pre>
        </template>

        <div v-if="!hasRecipe" class="block-head">
          <h3>Launch extras</h3>
          <InfoHint>Env vars and bind mounts every container in this campaign gets.</InfoHint>
          <span class="spacer" />
          <el-button size="small" text type="primary"
            @click="copyText(extrasYaml, 'Launch extras copied')">Copy YAML</el-button>
        </div>
        <pre v-if="!hasRecipe" class="mono block">{{ hasExtras ? extrasYaml : '(none)' }}</pre>

        <div class="block-head">
          <h3>Difference from production</h3>
          <InfoHint>Flags production's recorded baseline sets that this campaign never
            sets.</InfoHint>
        </div>
        <pre v-if="hasRecipe" class="mono block">none: the recipe is production's config</pre>
        <pre v-else class="mono block">{{ parity?.missing?.length
          ? parity.missing.map((m) => `${m.flag}: ${m.production}`).join('\n')
          : (parity?.baseline_id ? 'none' : 'no baseline recorded for this model') }}</pre>

        <div class="block-head">
          <h3>Search space</h3>
          <span class="muted tiny">{{ spaceName || (spaceSaved ? `saved as ${spaceSaved}` : 'written for this campaign') }}</span>
          <span class="spacer" />
          <el-button v-if="!spaceName && !spaceSaved" size="small" text type="primary" @click="saveSpace">
            Save as search space</el-button>
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
              <el-tag v-if="row.is_baseline" size="small" type="info" effect="plain"
                class="baseline-tag" title="Production's config, measured as the reference">
                {{ t('campaign.baselineRow') }}</el-tag>
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

    <el-dialog v-model="editingSchedule" :title="t('campaign.scheduleTitle')" width="560px">
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

    <CatalogExportDialog v-model="catalogOpen" :candidates="catalogCandidates"
      :initial="catalogInitial" :model-name="campaign?.served_model_name" />
    <el-dialog v-model="exportOpen" :title="t('campaign.exportTitle')" width="760px">
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
        <div class="run-facts">
          <el-tag :type="runStatus(selectedRun.status).type">{{ runLabel(selectedRun.status) }}</el-tag>
          <span v-if="selectedRun.failure_class" class="mono">{{ failureLabel(selectedRun.failure_class) }}</span>
          <span class="muted" :title="exactTime(selectedRun.started_at)">
            <el-icon><Timer /></el-icon> {{ duration(selectedRun.started_at, selectedRun.finished_at) }}</span>
          <span v-if="selectedRun.gpu_indices?.length" class="muted mono">
            GPU {{ selectedRun.gpu_indices.join(',') }}</span>
          <span v-if="selectedRun.endpoint_url" class="muted mono">{{ selectedRun.endpoint_url }}</span>
          <span v-if="selectedRun.llmbench_submission_id" class="muted">
            LLMBench <span class="mono">#{{ selectedRun.llmbench_submission_id }}</span></span>
        </div>
        <el-alert v-if="selectedRun.error && selectedRun.status !== 'succeeded'"
          :type="selectedRun.status === 'failed' ? 'error' : 'info'" :closable="false"
          show-icon class="run-error" :title="selectedRun.status">
          <pre class="mono run-error-text">{{ selectedRun.error }}</pre>
        </el-alert>

        <div v-if="runVerdict" class="verdict">
          <div class="verdict-score">
            <span class="muted tiny">{{ runVerdict.label }}</span>
            <b class="headline">{{ fmtScore(runVerdict.value) }}</b>
            <span v-if="runVerdict.unit" class="muted tiny">{{ runVerdict.unit }}</span>
          </div>
          <div class="verdict-side">
            <span :class="runVerdict.feasible ? 'ok-text' : 'fail-text'">
              <el-icon><CircleCheckFilled v-if="runVerdict.feasible" /><CircleCloseFilled v-else /></el-icon>
              {{ runVerdict.feasible ? t('campaign.redlinesHeld') : t('campaign.redlinesCrossed') }}
            </span>
            <span v-if="!runVerdict.feasible" class="muted tiny">
              {{ runVerdict.breaches.join('; ') || 'no score' }}</span>
            <span class="muted tiny">
              <el-tag size="small" effect="plain"
                :type="runVerdict.stage === 'verify' ? 'warning' : 'info'">
                {{ runVerdict.stage === 'verify' ? 'replay' : (staged ? 'screening' : 'benchmark') }}
              </el-tag>
              <span class="mono"> {{ runVerdict.benchmark }}</span>
            </span>
          </div>
        </div>
        <div v-if="runKeyMetrics.length" class="key-metrics">
          <div v-for="m in runKeyMetrics" :key="m.key" class="metric-row">
            <span class="mono muted">{{ m.key }}</span>
            <span class="mono val">{{ fmtMetric(m.value) }}</span>
          </div>
        </div>

        <h3>{{ t('common.config') }}</h3>
        <ConfigChips :config="selectedRun.config" :keys="sweptKeys" />

        <h3>{{ t('campaign.launchConfig') }}</h3>
        <el-tabs v-model="launchTab" type="border-card" class="launch-tabs">
          <el-tab-pane :label="t('runs.serveTab')" name="serve">
            <div class="log-head">
              <span class="muted tiny">{{ t('runs.serveNote') }}</span>
              <CopyButton :value="runLaunch?.rendered.serve_command || selectedRun.serve_command || ''"
                :label="t('runs.engineCommandCopied')" />
            </div>
            <pre class="mono block">{{ runLaunch?.rendered.serve_command || selectedRun.serve_command || '—' }}</pre>
          </el-tab-pane>
          <el-tab-pane :label="t('runs.engineArgsTab')" name="args">
            <div class="log-head">
              <span class="muted tiny">{{ t('runs.engineArgsNote') }}</span>
              <CopyButton :value="engineArgsJson" :label="t('runs.engineArgsCopied')" />
            </div>
            <pre class="mono block">{{ engineArgsJson }}</pre>
          </el-tab-pane>
          <el-tab-pane v-if="launchYaml" :label="t('runs.launchConfigTab')" name="yaml">
            <div class="log-head">
              <span class="muted tiny">{{ t('runs.launchConfigNote') }}</span>
              <CopyButton :value="launchYaml" :label="t('runs.launchConfigCopied')" />
            </div>
            <pre class="mono block">{{ launchYaml }}</pre>
          </el-tab-pane>
          <el-tab-pane :label="t('runs.appliedTab')" name="applied">
            <div class="log-head">
              <span class="muted tiny">{{ t('runs.appliedNote') }}</span>
              <CopyButton v-if="appliedCommand" :value="appliedCommand" :label="t('runs.appliedCopied')" />
            </div>
            <pre class="mono block">{{ appliedCommand || t('runs.notLaunched') }}</pre>
            <dl class="run-image">
              <dt>Image</dt><dd class="mono">{{ runLaunch?.config.image || selectedRun.image }}</dd>
              <dt>Weights</dt><dd class="mono">{{ runLaunch?.config.model_path || selectedRun.model_path }}</dd>
            </dl>
          </el-tab-pane>
        </el-tabs>

        <el-collapse class="run-more">
          <el-collapse-item v-for="(r, i) in selectedRun.results" :key="i" :name="`result-${i}`">
            <template #title>
              {{ t('runs.metrics') }} · {{ r.source }}
              <span class="muted tiny metric-count">{{ t('runs.metricCount', { n: Object.keys(r.metrics ?? {}).length }) }}</span>
            </template>
            <pre class="mono block small">{{ JSON.stringify(r.metrics, null, 2) }}</pre>
          </el-collapse-item>
        </el-collapse>

        <div class="log-head">
          <h3>{{ t('runs.capturedLog') }}</h3>
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
  <PageLoading v-else :error="first.error" @retry="first.run" />
</template>

<style scoped>
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
/* el-button's sibling margin is for loose buttons; the flex gap spaces these. */
.actions > .el-button + .el-button {
  margin-left: 0;
}
.run-control {
  margin-left: 8px;
}
.run-control .stop {
  color: var(--el-color-danger);
}
.run-control .stop-request {
  color: var(--el-color-warning-dark-2);
}
.run-control .stop:hover,
.run-control .stop:focus-visible {
  color: #fff;
  background: var(--el-color-danger);
  border-color: var(--el-color-danger);
}
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
.session-track {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--autotune-muted);
}
.session-step {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.session-step .pip {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 1px solid var(--autotune-border);
}
.session-step.done .pip {
  background: var(--el-color-success);
  border-color: var(--el-color-success);
}
.session-step.now {
  color: var(--el-text-color-primary);
  font-weight: 600;
}
.session-step.now .pip {
  background: var(--el-color-primary);
  border-color: var(--el-color-primary);
}
.session-step.bad .pip {
  background: var(--el-color-danger);
  border-color: var(--el-color-danger);
}
.session-warn {
  color: var(--el-color-warning-dark-2, #b88230);
}
.session-bad {
  color: var(--el-color-danger);
}
.run-error {
  margin-bottom: 12px;
}
.run-error-text {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 4px 0;
  font-size: 12px;
  max-height: 160px;
  overflow: auto;
}
.run-image {
  display: grid;
  grid-template-columns: 70px 1fr;
  gap: 4px 10px;
  margin: 8px 0 4px;
  font-size: 12px;
}
.run-image dt {
  color: var(--autotune-muted);
}
.run-image dd {
  margin: 0;
  word-break: break-all;
}
.clickable :deep(.el-table__row) {
  cursor: pointer;
}
.launch-collapse {
  margin: 12px 0;
}
.title-block {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.title-block .page-title {
  margin: 0;
}
.status-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  animation: pulse 1.6s ease-in-out infinite;
}
@keyframes pulse {
  50% {
    opacity: 0.3;
  }
}
.owner {
  margin-left: 2px;
}
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 14px 22px;
  padding: 14px 18px;
  margin-bottom: 12px;
  background: #fff;
  border: 1px solid var(--autotune-border);
  border-radius: 8px;
}
.facts .dir {
  vertical-align: -2px;
  color: var(--el-color-primary);
}
.progress {
  display: flex;
  align-items: center;
  gap: 8px;
}
.progress .el-progress {
  flex: 1;
  min-width: 50px;
}
.strip-icon {
  color: var(--el-color-primary);
}
.session-strip {
  background: #fff;
  border: 1px solid var(--autotune-border);
  border-radius: 8px;
  padding: 8px 14px;
  margin-bottom: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.session-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.session-line .el-button + .el-button {
  margin-left: 0;
}
.session-line .risky {
  color: var(--el-color-danger);
}
.session-details {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 14px;
  margin: 6px 0 2px;
  padding-top: 8px;
  border-top: 1px solid var(--autotune-border);
  font-size: 12.5px;
}
.session-details dt {
  color: var(--autotune-muted);
}
.session-details dd {
  margin: 0;
  word-break: break-word;
}
.session-warn,
.session-bad {
  display: flex;
  align-items: center;
  gap: 6px;
}
.preflight-label {
  font-size: 13px;
}
.counts span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.chip-plain {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px;
  line-height: 22px;
  font-size: 12px;
  border: 1px solid var(--autotune-border);
  border-radius: 4px;
  background: #fff;
  color: var(--el-text-color-regular);
}
.chip-plain.redline {
  font-size: 11.5px;
}
.board-head {
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 10px;
}
.board-head.screening {
  margin-top: 26px;
}
.held {
  color: var(--el-color-success);
  font-size: 16px;
}
.crossed {
  color: var(--el-color-danger);
  font-size: 16px;
  cursor: help;
}
.run-facts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  font-size: 12.5px;
}
.run-facts .el-icon {
  vertical-align: -2px;
}
.verdict {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 12px 14px;
  margin-bottom: 10px;
  background: var(--autotune-bg);
  border: 1px solid var(--autotune-border);
  border-radius: 6px;
}
.verdict-score {
  display: flex;
  flex-direction: column;
}
.verdict-score .headline {
  font-size: 22px;
  line-height: 1.2;
}
.verdict-side {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.verdict-side .el-icon {
  vertical-align: -2px;
}
.run-more {
  margin: 14px 0;
}
.metric-count {
  margin-left: 8px;
}

/* The board's answer, above its evidence. */
.winner-card {
  border: 1px solid var(--el-color-success-light-5, #b3e19d);
  background: var(--el-color-success-light-9, #f0f9eb);
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 16px;
}
.winner-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}
.winner-badge {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--el-color-success-dark-2, #529b2e);
}
.winner-score {
  font-size: 18px;
}
.winner-actions {
  margin-left: auto;
}
.winner-config {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}
.launch-tabs {
  margin-bottom: 12px;
}
.launch-tabs pre.block {
  margin: 0;
}
</style>
