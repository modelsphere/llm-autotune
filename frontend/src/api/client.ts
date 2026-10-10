import axios from 'axios'

export const api = axios.create({ baseURL: '/api' })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('autotune_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && location.pathname !== '/login') {
      localStorage.removeItem('autotune_token')
      location.href = '/login'
    }
    return Promise.reject(error)
  },
)

// -- typed helpers -----------------------------------------------------------

export interface Machine {
  id: number
  name: string
  host: string
  ssh_user: string
  ssh_port: number
  gpu_count: number
  gpu_type: string
  /** Substrate this machine is reached through. '' = platform default
   *  (ssh_docker today); 'k8s' = a slice of the GPU cluster via the k8s API. */
  driver: string
  /** k8s only: `label=value` pairs pinning this slice's pods to nodes (e.g. a
   *  node that carries the model's weights). Empty = the scheduler is free. */
  node_selector: string
  /** Where the ENGINE reaches this box for inter-node traffic (dist-init /
   *  NCCL), when that is not `host`. `host` is the ssh target and the address
   *  LLMBench is given; a rail NIC is often a different one. '' = `host`. */
  data_host: string
  /** NCCL_SOCKET_IFNAME, when the routable IP and the rail differ. '' = default.
   *  Usually set on the group (`nccl_env`) rather than per machine. */
  nccl_ifname: string
  state: 'away' | 'available' | 'reserved'
  notes: string
  gpus_busy: number
  /** Who lent us this machine and until when. `none` = never leased. */
  lease_state: LeaseState
  lease_holder: string
  lease_note: string
  leased_at: string | null
  lease_due_at: string | null
  lease_end_mode: '' | 'polite' | 'eager'
  lease_deadline_at: string | null
  lease_released_at: string | null
  /** Node group this machine belongs to, and its rank in it (0 = master).
   *  '' = not grouped. Membership is a topology, not a reservation: a grouped
   *  machine is still schedulable for single-node work. */
  group: string
  group_rank: number | null
  /** k8s only: which cluster row this machine lands in. null = the platform
   *  default cluster (the AUTOTUNE_K8S_* env). */
  cluster_id: number | null
  /** The cluster's name for display; '' = the default cluster. */
  cluster_name: string
}

/** A Kubernetes GPU cluster the platform may launch onto. The kubeconfig is
 *  write-only: it goes in as plaintext and the API returns only
 *  `has_kubeconfig`, never the credential. `cluster_id == null` on a machine
 *  always means the platform-default cluster, whose values are the
 *  AUTOTUNE_K8S_* settings — so a single-cluster deployment never needs a row. */
export interface Cluster {
  id: number
  name: string
  notes: string
  api_mode: 'client' | 'kubectl' | 'unavailable'
  has_kubeconfig: boolean
  context: string
  namespace: string
  workload_kind: 'deployment' | 'custom'
  /** Host LLMBench is given for a run's NodePort endpoint. Empty = derive from
   *  a node InternalIP, which needs the cluster-scoped `nodes` read. */
  node_host: string
  gpu_resource: string
  runtime_class: string
  tolerate_gpu_taint: boolean
  tolerations: string
  image_pull_secrets: string
  model_pvc: string
  model_pvc_root: string
  shm_size_mb: number
  node_selector: string
  service_nodeport: number
  run_ttl_seconds: number
  in_cluster: boolean
  cr_group: string
  cr_version: string
  cr_kind: string
  cr_plural: string
  cr_pod_label: string
  engine_cpu_request: string
  engine_memory_request: string
  engine_cpu_limit: string
  engine_memory_limit: string
  /** Result of the last capability probe, rendered on the Resources page. */
  last_probe: Record<string, any>
  last_probe_at: string | null
  created_at: string | null
  /** How many machines name this cluster. */
  machine_count: number
}

/** The editable subset. `kubeconfig` is plaintext in, never out; '' on edit
 *  keeps the stored credential. */
export interface ClusterInput {
  name: string
  notes?: string
  api_mode?: string
  kubeconfig?: string
  context?: string
  namespace?: string
  workload_kind?: string
  node_host?: string
  gpu_resource?: string
  runtime_class?: string
  tolerate_gpu_taint?: boolean
  tolerations?: string
  image_pull_secrets?: string
  model_pvc?: string
  model_pvc_root?: string
  shm_size_mb?: number
  node_selector?: string
  service_nodeport?: number
  run_ttl_seconds?: number
  in_cluster?: boolean
  engine_cpu_request?: string
  engine_memory_request?: string
  engine_cpu_limit?: string
  engine_memory_limit?: string
}

/** A node group: the multi-node resource. A named, ordered set of machines that
 *  can be deployed as ONE gang, with `members[0]` the master. */
export interface MachineGroupMember {
  name: string
  rank: number
  is_master: boolean
  host: string
  /** The address the engine uses for inter-node traffic; falls back to `host`. */
  data_host: string
  gpu_count: number
  gpu_type: string
  driver: string
  leased: boolean
  state: string
  needs_attention: boolean
  gpus_busy: number
  /** When this member's lease is promised back. */
  lease_due_at: string | null
}

export interface MachineGroup {
  id: number
  name: string
  driver: string
  nccl_env: Record<string, string>
  dist_port: number
  extra_env: Record<string, string>
  extra_volumes: Record<string, string>
  notes: string
  members: MachineGroupMember[]
  node_count: number
  /** Every member leased, usable and cleared — i.e. deployable right now.
   *  Advisory: the scheduler re-checks at launch. */
  deployable: boolean
  /** One line per reason it is not deployable ("node-26 is not leased"). */
  blockers: string[]
  /** Non-blocking: a member's lease runs out soon, so a gang could be cut short. */
  warnings: string[]
  created_at: string | null
}

/** One machine's preflight row, from `POST /machine-groups/{id}/preflight`. */
export interface PreflightRow {
  machine: string
  ok: boolean
  failed: number
  warnings: number
  checks: {
    key: string
    label: string
    status: 'pass' | 'warn' | 'fail' | 'skip'
    detail: string
    hint: string
  }[]
}

export interface MachineGroupPreflight {
  machines: PreflightRow[]
  ok: boolean
  note: string
}

export interface MachineGroupInput {
  name?: string
  members: string[]
  driver?: string
  nccl_env?: Record<string, string>
  dist_port?: number
  extra_env?: Record<string, string>
  extra_volumes?: Record<string, string>
  notes?: string
}

export type LeaseState = 'none' | 'active' | 'draining' | 'released'

/** The external view of a machine, from `/machines/{name}/lease`. Shaped for a
 *  fleet manager rather than for this UI, which is why `readiness` collapses
 *  several of our internal states into the one word a caller acts on. */
export interface LeaseStatus {
  machine: string
  host: string
  gpu_count: number
  gpus_in_use: number
  state: string
  lease_state: LeaseState
  lease_holder: string
  leased_at: string | null
  lease_due_at: string | null
  lease_end_mode: string
  lease_deadline_at: string | null
  lease_released_at: string | null
  readiness: 'busy' | 'idle' | 'returnable'
  returnable_at: string | null
  live_runs: {
    run_id: number
    campaign_id: number
    status: string
    gpus: number[]
    started_at: string | null
  }[]
  stage: { headline: string; detail: string; step: number; state: string }
}

export interface ApiKey {
  id: number
  name: string
  prefix: string
  masked: string
  last_used_at: string | null
  revoked_at: string | null
  created_at: string
}

/** Only ever returned by the mint call — the plaintext exists in one response
 *  and is never recoverable afterwards. */
export interface ApiKeyCreated extends ApiKey {
  secret: string
}

/** What `/campaigns/{id}/schedule` answers, so nobody has to do the midnight
 *  arithmetic in the browser. */
export interface CampaignSchedule {
  daily_start: string
  daily_end: string
  timezone: string
  schedule_until: string | null
  summary: string
  overnight: boolean
  window_start: string | null
  window_end: string | null
  current_window: { start: string; end: string } | null
  next_window: { start: string; end: string } | null
  finished: boolean
}

/** Where a machine is in its lease, from `/machines/lifecycle` — the same
 *  predicates the worker acts on, so the page never guesses. */
export interface MachineLifecycle {
  machine_id: number
  /** Index into the `steps` array the same endpoint returns. */
  step: number
  state: 'waiting' | 'working' | 'blocked' | 'done'
  headline: string
  detail: string
  campaigns: { id: number; name: string }[]
  /** The same word the lease API gives an external caller, so the page and the
   *  fleet manager can never describe a machine differently. */
  readiness: 'busy' | 'idle' | 'returnable'
  /** Worst case for when a polite hand-back completes; null when idle. */
  returnable_at: string | null
  /** What End lease does on this machine right now, in one line. */
  hand_back: { summary: string }
}

export interface Campaign {
  id: number
  owner_id: number
  /** What installed plugins keep about the campaign, by plugin name. */
  extensions?: Record<string, Record<string, unknown>>
  name: string
  engine: string
  image: string
  model_path: string
  served_model_name: string
  search_space: Record<string, unknown>
  objective: Record<string, unknown>
  status: string
  /** The benchmark every candidate runs. Single-stage replay is the default:
   *  this holds the replay slug and nothing else runs. When the verify stage is
   *  opted into, this becomes the cheaper screen instead. No platform default —
   *  it is always set per model. */
  benchmark_slug: string
  service_port: number
  machine_names: string[]
  /** A node group each run deploys ACROSS, by name. '' = single-node and
   *  `machine_names` is the whole pin. Non-empty: the group's members are the
   *  pin and their count is the deployment width. sglang only for now. */
  node_group: string
  run_baseline_canary: boolean
  share_machine: boolean
  extra_env: Record<string, string>
  extra_volumes: Record<string, string>
  window_start: string | null
  window_end: string | null
  /** The recurring window: "23:00" to "08:00" in `schedule_timezone`. Empty
   *  means the campaign has no clock and is driven by hand. */
  daily_start: string
  daily_end: string
  schedule_timezone: string
  schedule_until: string | null
  /** Set by Force start: run regardless of the clock until this instant. */
  override_until: string | null
  max_run_minutes: number
  /** An external policy container (policy-as-code) that decides what to try.
   *  Null: the campaign tries every configuration in the space, in order. The
   *  platform launches, benchmarks and judges either way. */
  policy_id: number | null
  policy_settings: Record<string, unknown>
  /** What this campaign's runs have taken so far, in minutes; null until a
   *  run has been measured. The window is planned from these. */
  learned_timing: {
    startup_minutes: number | null
    bench_minutes: number | null
    verify_bench_minutes: number | null
    samples: number
    /** From the benchmark's own settings, before any run is measured. */
    estimated_bench_minutes: number | null
  }
  confirm_top_k: number
  confirm_repeats: number
  /** The opt-in second stage, off by default. Set both the slug and top_k > 0 to
   *  screen every candidate on `benchmark_slug` first, then run this benchmark
   *  only on the best K. Empty slug or top_k 0 = single-stage: `benchmark_slug`
   *  alone (the default replay). */
  verify_benchmark_slug: string
  verify_top_k: number
  verify_objective: Record<string, unknown>
  verify_max_run_minutes: number
  /** The replay dataset this campaign is held to. `dataset_profile` is what was
   *  asked for; the rest is what it actually pinned, once it started. */
  dataset_profile: string
  dataset_policy: string
  dataset_build_id: string
  dataset_sha256: string
  dataset_policy_applied: string
  dataset_pinned_at: string | null
  /** What the search space expands to. Declared, not planned: the platform only
   *  creates candidate rows once the campaign is active. */
  candidate_count: number
  created_at: string
  /** Row-only lease findings about this campaign's pinned machines, computed at
   *  read time. Empty when every pinned machine is leased with enough runway. */
  machine_warnings: MachineWarning[]
}

/** A row-only finding about a machine a run is pinned to — no ssh, just the
 *  lease table. Shown as a banner before anything launches. */
export interface MachineWarning {
  /** '' for a fleet-wide finding (no pool pinned, nothing leased at all). */
  machine: string
  status: 'unavailable' | 'expiring' | 'expired'
  detail: string
}

// -- preflight ----------------------------------------------------------------

/** One machine-side (or shared) check in a preflight run. */
export interface PreflightCheck {
  key: string
  label: string
  status: 'pass' | 'warn' | 'fail' | 'skip'
  detail: string
  hint: string
}

/** Every check for one machine, plus its rollup. */
export interface PreflightMachine {
  machine: string
  ok: boolean
  failed: number
  warnings: number
  checks: PreflightCheck[]
}

/** A whole preflight: per-machine results, an overall verdict, and a fleet-wide
 *  note (e.g. nothing leased at all). */
export interface PreflightResult {
  machines: PreflightMachine[]
  ok: boolean
  note: string
  machine_warnings?: MachineWarning[]
}

export interface Run {
  id: number
  campaign_id: number
  candidate_id: number
  machine_id: number | null
  kind: string // experiment | policy_launch | external (| baseline, before 0.2.0)
  /** screen = the cheap benchmark every candidate gets; verify = the expensive
   *  one only the shortlist gets. Their metrics share no names. */
  stage: string
  status: string
  gpu_indices: number[]
  service_port: number
  container_name: string
  endpoint_url: string
  launch_command: string
  llmbench_submission_id: string
  failure_class: string
  error: string
  /** Set when a policy session drove this run — links to its container log. */
  policy_session_id: number | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface RunDetail extends Run {
  config: Record<string, unknown>
  results: {
    id: number
    source: string
    passed: boolean
    score: number | null
    metrics: Record<string, unknown>
    /** The campaign objective resolved against these metrics when they landed —
     *  the number the search actually ranked this run by. */
    objective_value: number | null
    feasible: boolean
    breaches: string[]
    created_at: string
  }[]
}

export interface LeaderboardEntry {
  run_id: number
  config: Record<string, unknown>
  score: number | null
  metrics: Record<string, unknown>
  holds_redlines: boolean
  breaches: string[]
  /** Which stage measured this. Entries from different stages must never be
   *  ranked against each other — different benchmarks, different units. */
  stage: string
  /** The metric `score` actually is, so a table can label its own column. */
  target_metric: string
  /** False when this run replayed a different dataset build than the campaign
   *  pinned. The measurement is real; it answers a different question. */
  comparable: boolean
  dataset_build_id: string
  /** This entry's score ÷ the same-stage production baseline. Read with the
   *  objective's direction: on a maximize target > 1 beats production. null
   *  when no baseline ran this stage. The board ranks on it when present. */
  vs_baseline: number | null
  /** This row IS the production baseline, not a candidate. */
  is_baseline: boolean
  /** The canonical GPU type this run landed on (e.g. "H100"). Empty when the
   *  substrate could not say. A run on a different card than the stage's
   *  baseline is not normalized against it and ranks below the same-chip
   *  entries — per-card throughput is card-count, not card-type, normalized. */
  card_type: string
}

export interface CatalogParam {
  name: string
  type: 'int' | 'float' | 'bool' | 'str' | 'enum'
  help: string
  tunable: boolean
  choices: (string | number)[] | null
  min: number | null
  max: number | null
  example: (string | number)[] | null
}

/** A metric a benchmark reports, from `/objectives/metrics`. `better` says
 *  which direction is an improvement, so the editor can default the direction. */
export interface MetricSpec {
  key: string
  label: string
  better: 'higher' | 'lower'
  help: string
  unit: string
  headline: boolean
  /** Which benchmark reports it. Two benchmarks share no metric names, so an
   *  objective mixing groups names keys one of them will never return. */
  group: string
}

/** A limit a run must stay inside to be eligible at all, however fast it was. */
export interface Redline {
  metric: string
  op: string
  value: number
}

/** A replay collection profile on LLMBench — the traffic sample the expensive
 *  stage measures against. `managed` means nothing rebuilds it on a schedule,
 *  which is the only kind a campaign can hold for its whole life. */
export interface DatasetProfile {
  name: string
  display_name: string
  /** Resampled only when a campaign asks, so it holds steady in between. */
  managed: boolean
  /** How often LLMBench resamples it on its own; 0 = only on request. */
  schedule_hours: number
  enabled: boolean
  build_id: string
  records: number | null
  built_at: string
  window_start: string
  window_end: string
}

export interface Objective {
  id: number
  owner_id: number | null
  name: string
  description: string
  target_metric: string
  direction: 'maximize' | 'minimize'
  redlines: Redline[]
  is_builtin: boolean
  created_at: string
  updated_at: string
}

export interface SearchSpace {
  id: number
  owner_id: number
  name: string
  engine: string
  description: string
  base: Record<string, unknown>
  grid: Record<string, unknown[]>
  /** Groups swept together rather than crossed: {param: [values]}, zipped. */
  tied: Record<string, unknown[]>[]
  /** Parameters swept over an interval: {param: {min, max, step}}. The step is
   *  mandatory server-side — it is what keeps the space countable. */
  range: Record<string, Record<string, unknown>>
  /** Parameters that only apply for certain values of others:
   *  {param: {gating_param: [values]}}. An inactive parameter is dropped
   *  before hashing, so configs that deploy identically are one candidate. */
  conditions: Record<string, Record<string, unknown[]>>
  /** Computed server-side from the space itself — never recounted here. */
  candidate_count: number
  created_at: string
  updated_at: string
}

export interface SearchSpacePreview {
  candidate_count: number
  invalid: { config: Record<string, unknown>; error: string }[]
  gpu_count_used: number
  sample: Record<string, unknown>[]
  /** Mistakes in the space itself — these block saving, unlike `invalid`. */
  errors: string[]
}

export interface Candidate {
  id: number
  config: Record<string, unknown>
  status: string
  validation_error: string
  cards: number
}

/** A production reference config, keyed by (served model, engine, card type).
 *  Campaigns compare their candidates against it. Managed on the Baselines
 *  page or upserted automatically when a machine is handed over. */
export interface Baseline {
  id: number
  served_model_name: string
  engine: string
  card_type: string
  engine_args: Record<string, unknown>
  /** The rest of the launch config — a baseline is a whole one, not just the
   *  knobs, so it converts losslessly to a campaign and the
   *  deploy repo's file. */
  image: string
  model_path: string
  service_port: number
  extra_env: Record<string, string>
  extra_volumes: Record<string, string>
  weights_fingerprint: string
  /** GPUs the config occupies, derived like a candidate's. */
  cards: number
  /** "manual", "capture:<machine>", or "<host>:<branch>@<sha>" when synced
   *  from a deploy repo. */
  source: string
  notes: string
  /** Where this config lives in git, when bound. */
  created_at: string
  updated_at: string | null
}

/** A registered external search-policy container, from `/policies`. A campaign
 *  that names one searches with it: the container proposes every config, and
 *  the platform stops enumerating the declared space itself. */
export interface Policy {
  id: number
  owner_id: number
  name: string
  description: string
  image: string
  repo_url: string
  version: string
  gpus_in_container: boolean
  /** Mount the campaign's weights at /model. */
  needs_model: boolean
  env: Record<string, string>
  ports: number
  created_at: string | null
}

// -- logs ---------------------------------------------------------------------

/** One selectable log in the viewer dialog — the policy container, or any run's
 *  engine container. */
export interface LogSource {
  /** Stable id for the picker. */
  key: string
  /** Human label, e.g. "Policy container" or "Engine · run 788 (verify)". */
  label: string
  fetch: () => Promise<string>
  /** Suggested download filename. */
  filename: string
}

/** One run's captured container log (stdout+stderr, merged by `docker logs`).
 *  `responseType: 'text'` so a plain-text body is never JSON-parsed. */
export async function fetchRunLog(runId: number): Promise<string> {
  return (await api.get(`/runs/${runId}/log`, { responseType: 'text' })).data
}

/** The policy container's stdout+stderr — live from the container while the
 *  session runs, the file captured at teardown once it has ended. */
/** One policy container's night (GET /policy-sessions/{id}). */
export interface PolicySessionDetail {
  id: number
  status: 'pending' | 'starting' | 'searching' | 'finalizing' | 'validating' | 'done'
    | 'failed' | 'aborted'
  started_at: string | null
  first_heartbeat_at: string | null
  last_heartbeat_at: string | null
  policy_phase: string
  policy_message: string
  policy_progress: number | null
  search_end_reason: string
  failure_class: string
  error: string
  finalize_requested_at: string | null
  abort_requested_at: string | null
  search_deadline: string | null
  trials: unknown[]
  contenders: { id: number; status?: string }[]
  waiting: string
}

export async function fetchPolicySessionLog(sessionId: number): Promise<string> {
  return (await api.get(`/policy-sessions/${sessionId}/log`, { responseType: 'text' })).data
}

/** Hand the browser a text file to download (log export). */
export function downloadText(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

// -- agent API (reports an LLM wrote from finished runs) -----------------------

/** Runs a plugin keeps together as one comparable set. */
export interface RunGroup {
  kind: string
  slug: string
  name: string
  model_name: string
  served_model_name: string
  precision: string
  gpu_type: string
  /** The group's page in this app, or '' when it has none. */
  page_path: string
}

export interface AgentReport {
  id: number
  title: string
  baseline_run_id: number
  attempt_run_ids: number[]
  campaign_id: number | null
  /** The group every run of the comparison belongs to, when there is one. */
  group?: RunGroup | null
  comparable: boolean
  generator: Record<string, unknown>
  created_by_name: string
  created_at: string | null
  asset_names: string[]
  url: string
  lang: string
  translation_of: number | null
  labels: string[]
  /** What this report calls its scenarios, per scenario key. */
  scenario_labels: Record<string, { name?: string; description?: string }>
}

export interface AgentReportDetail extends AgentReport {
  markdown: string
  comparison: Record<string, unknown>
  /** Every language this report exists in, itself included. */
  translations: { id: number; lang: string; title: string }[]
}
