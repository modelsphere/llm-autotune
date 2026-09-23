<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Cluster, type Machine, type MachineGroup, type MachineGroupPreflight, type MachineLifecycle } from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import { relativeTime } from '../utils/time'

/** Reaching `window` from a template needs it bound explicitly under
 *  `<script setup>`; the component instance is not the global object. */
const openDocs = () => window.open('/api/docs', '_blank')

const router = useRouter()
const machines = ref<Machine[]>([])
const groups = ref<MachineGroup[]>([])
/** The Kubernetes clusters a k8s machine may land in. `cluster_id === null` on
 *  a machine means the platform-default cluster, which has no row here. */
const clusters = ref<Cluster[]>([])
const steps = ref<string[]>([])
const autoLifecycle = ref(true)
/** Whether WE put production back when a lease ends. Off by default, and the
 *  one setting an operator must not discover after pressing End lease. */
const autoRestore = ref(true)
const lifecycle = ref<Record<number, MachineLifecycle>>({})
const showAdd = ref(false)
const busy = ref(false)
let timer: number | undefined

const emptyForm = () => ({
  name: '',
  host: '',
  ssh_user: 'root',
  ssh_port: 22,
  gpu_count: 8,
  gpu_type: '',
  driver: '',
  cluster_id: null as number | null,
  node_selector: '',
  data_host: '',
  nccl_ifname: '',
  notes: '',
})
const form = ref(emptyForm())
/** null = the dialog is adding; a number = editing that machine. */
const editingId = ref<number | null>(null)

/** -- node groups ----------------------------------------------------------
 *
 * A group is a named topology over leased machines, not a reservation: forming
 * one leaves every member an ordinary machine the single-node scheduler may
 * still use. The page says so where an operator would otherwise assume
 * otherwise — grouped machines keep their own Lease / End lease buttons.
 */
const showGroup = ref(false)
const editingGroupId = ref<number | null>(null)
const emptyGroupForm = () => ({
  name: '',
  members: [] as string[],
  driver: '',
  dist_port: 0,
  ncclEnvText: '',
  notes: '',
})
const groupForm = ref(emptyGroupForm())

/** -- clusters --------------------------------------------------------------
 *
 * The apiservers a k8s machine may land in. A worker serves several at once,
 * which is why a machine carries a cluster and the old process-wide
 * AUTOTUNE_K8S_* settings are now only the DEFAULT cluster (a machine with no
 * cluster). The kubeconfig is write-only: paste it, it is stored encrypted,
 * and Edit shows "credential stored" with an empty box meaning "keep it".
 */
const showClusters = ref(false)
const editingClusterId = ref<number | null>(null)
const emptyClusterForm = () => ({
  name: '',
  api_mode: 'client',
  kubeconfig: '',
  namespace: 'autotune',
  workload_kind: 'deployment',
  node_host: '',
  gpu_resource: 'nvidia.com/gpu',
  runtime_class: 'nvidia',
  shm_size_mb: 2048,
  node_selector: '',
  tolerations: '',
  image_pull_secrets: '',
  engine_cpu_request: '',
  engine_memory_request: '',
  engine_cpu_limit: '',
  engine_memory_limit: '',
  notes: '',
})
const clusterForm = ref(emptyClusterForm())
const probingCluster = ref<number | null>(null)

/** Group preflight: the per-machine probes plus the interconnect a gang adds —
 *  boxes can each be perfect and still be unable to reach each other, which no
 *  per-machine view shows. */
const showGroupPreflight = ref(false)
const groupPreflight = ref<MachineGroupPreflight | null>(null)
const preflightGroup = ref('')
const checkTag: Record<string, string> = {
  pass: 'success',
  warn: 'warning',
  fail: 'danger',
  skip: 'info',
}

async function runGroupPreflight(group: MachineGroup) {
  busy.value = true
  preflightGroup.value = group.name
  groupPreflight.value = null
  showGroupPreflight.value = true
  try {
    // No image or weights: this is the fabric check. Engine-specific checks
    // (image, model path) come from the campaign preflight, which has them.
    const { data } = await api.post(`/machine-groups/${group.id}/preflight`, {})
    groupPreflight.value = data
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Preflight failed')
    showGroupPreflight.value = false
  } finally {
    busy.value = false
  }
}

/** Group membership is already on the machine rows, so the picker can show
 *  which boxes are free to join and which are spoken for. */
const memberChoices = computed(() =>
  machines.value.map((m) => ({
    name: m.name,
    label: `${m.name} · ${m.gpu_count}×${m.gpu_type || 'GPU'} · ${
      m.lease_state === 'active' ? 'leased' : m.state
    }${m.group ? ` · in ${m.group}` : ''}`,
    disabled: Boolean(m.group) && m.group !== groupForm.value.name,
  })),
)

async function load() {
  machines.value = (await api.get('/machines')).data
  try {
    groups.value = (await api.get('/machine-groups')).data
  } catch {
    groups.value = [] // the fleet list is the page's job; groups are extra
  }
  try {
    clusters.value = (await api.get('/clusters')).data
  } catch {
    clusters.value = [] // clusters are an overlay on the machine list
  }
  const { data } = await api.get('/machines/lifecycle')
  steps.value = data.steps
  autoLifecycle.value = data.auto
  autoRestore.value = data.auto_restore
  lifecycle.value = Object.fromEntries(
    (data.machines as MachineLifecycle[]).map((m) => [m.machine_id, m]),
  )
}

function stageOf(machine: Machine): MachineLifecycle | undefined {
  return lifecycle.value[machine.id]
}

function openAdd() {
  editingId.value = null
  form.value = emptyForm()
  showAdd.value = true
}

function openEdit(machine: Machine) {
  editingId.value = machine.id
  form.value = {
    name: machine.name,
    host: machine.host,
    ssh_user: machine.ssh_user,
    ssh_port: machine.ssh_port,
    gpu_count: machine.gpu_count,
    gpu_type: machine.gpu_type,
    driver: machine.driver,
    cluster_id: machine.cluster_id ?? null,
    node_selector: machine.node_selector,
    data_host: machine.data_host ?? '',
    nccl_ifname: machine.nccl_ifname ?? '',
    notes: machine.notes,
  }
  showAdd.value = true
}

function openAddGroup() {
  editingGroupId.value = null
  groupForm.value = emptyGroupForm()
  showGroup.value = true
}

function openEditGroup(group: MachineGroup) {
  editingGroupId.value = group.id
  groupForm.value = {
    name: group.name,
    members: group.members.map((m) => m.name),
    driver: group.driver,
    dist_port: group.dist_port,
    ncclEnvText: Object.entries(group.nccl_env)
      .map(([key, value]) => `${key}=${value}`)
      .join('\n'),
    notes: group.notes,
  }
  showGroup.value = true
}

/** "KEY=VALUE" lines, the shape an operator copies out of a shell. */
function parseEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const at = trimmed.indexOf('=')
    if (at <= 0) continue
    out[trimmed.slice(0, at).trim()] = trimmed.slice(at + 1).trim()
  }
  return out
}

async function saveGroup() {
  busy.value = true
  try {
    const payload = {
      members: groupForm.value.members,
      driver: groupForm.value.driver,
      dist_port: groupForm.value.dist_port,
      nccl_env: parseEnv(groupForm.value.ncclEnvText),
      notes: groupForm.value.notes,
    }
    if (editingGroupId.value === null) {
      await api.post('/machine-groups', { name: groupForm.value.name, ...payload })
    } else {
      await api.put(`/machine-groups/${editingGroupId.value}`, payload)
    }
    showGroup.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Save failed')
  } finally {
    busy.value = false
  }
}

async function removeGroup(group: MachineGroup) {
  try {
    await ElMessageBox.confirm(
      `Dissolve ${group.name}? The machines are not touched — they simply go back ` +
        'to being loose machines in the fleet.',
      'Dissolve node group',
      { confirmButtonText: 'Dissolve', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/machine-groups/${group.id}`)
    ElMessage.success(`Dissolved ${group.name}`)
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Could not dissolve the group')
  }
}

function openClusters() {
  editingClusterId.value = null
  clusterForm.value = emptyClusterForm()
  showClusters.value = true
}

function openEditCluster(c: Cluster) {
  editingClusterId.value = c.id
  // kubeconfig stays EMPTY: the API never returns it, and empty on save means
  // "keep the stored credential".
  clusterForm.value = {
    name: c.name,
    api_mode: c.api_mode,
    kubeconfig: '',
    namespace: c.namespace,
    workload_kind: c.workload_kind,
    node_host: c.node_host,
    gpu_resource: c.gpu_resource,
    runtime_class: c.runtime_class,
    shm_size_mb: c.shm_size_mb,
    node_selector: c.node_selector,
    tolerations: c.tolerations,
    image_pull_secrets: c.image_pull_secrets,
    engine_cpu_request: c.engine_cpu_request,
    engine_memory_request: c.engine_memory_request,
    engine_cpu_limit: c.engine_cpu_limit,
    engine_memory_limit: c.engine_memory_limit,
    notes: c.notes,
  }
  showClusters.value = true
}

async function saveCluster() {
  busy.value = true
  try {
    const payload = { ...clusterForm.value }
    if (editingClusterId.value === null) await api.post('/clusters', payload)
    else await api.put(`/clusters/${editingClusterId.value}`, payload)
    ElMessage.success(`Saved ${payload.name}`)
    editingClusterId.value = null
    clusterForm.value = emptyClusterForm()
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Save failed')
  } finally {
    busy.value = false
  }
}

async function removeCluster(c: Cluster) {
  try {
    await ElMessageBox.confirm(
      `Remove cluster ${c.name}? Its kubeconfig is deleted with it. ` +
        'Machines that name it must be re-pointed first.',
      'Remove cluster',
      { confirmButtonText: 'Remove', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/clusters/${c.id}`)
    ElMessage.success(`Removed ${c.name}`)
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Could not remove the cluster')
  }
}

/** Read-only capability check: is the namespace reachable, can this credential
 *  read nodes (capacity + endpoint), is the operator's CRD installed. */
async function probeCluster(c: Cluster) {
  probingCluster.value = c.id
  try {
    const { data } = await api.post(`/clusters/${c.id}/probe`)
    const probe = data.probe ?? {}
    const warnings: string[] = probe.warnings ?? []
    const summary = probe.reachable
      ? `reachable · ${probe.node_count} node(s)`
      : 'not reachable'
    if (warnings.length) ElMessage.warning(`${c.name}: ${summary} — ${warnings.join('; ')}`)
    else ElMessage.success(`${c.name}: ${summary}`)
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Probe failed')
  } finally {
    probingCluster.value = null
  }
}

async function save() {
  busy.value = true
  try {
    const payload = { ...form.value }
    // On k8s the scheduler places pods, so host/ssh are unused — keep host
    // non-empty only so the machine list stays readable.
    if (payload.driver === 'k8s' && !payload.host.trim()) payload.host = 'k8s'
    if (editingId.value === null) await api.post('/machines', payload)
    else await api.put(`/machines/${editingId.value}`, payload)
    showAdd.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Save failed')
  } finally {
    busy.value = false
  }
}

async function removeMachine(machine: Machine) {
  try {
    await ElMessageBox.confirm(
      `Remove ${machine.name}? Its finished runs are kept; only the machine is deleted.`,
      'Remove machine',
      { confirmButtonText: 'Remove', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/machines/${machine.id}`)
    ElMessage.success(`Removed ${machine.name}`)
    await load()
  } catch (error: any) {
    // 409 carries the reason (campaigns pinned / live run) — show it verbatim.
    ElMessage.error(error.response?.data?.detail ?? 'Remove failed')
  }
}

/** Leasing from the UI goes through the same endpoint an external fleet
 *  manager calls — one code path, so a machine handed over by a person and one
 *  handed over by a service are in identical states afterwards. */
async function lease(machine: Machine) {
  try {
    await api.post('/machines/lease', {
      name: machine.name,
      host: machine.host,
      ssh_user: machine.ssh_user,
      ssh_port: machine.ssh_port,
      gpu_count: machine.gpu_count,
      gpu_type: machine.gpu_type,
      lease_note: 'leased from the Resources page',
    })
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Could not lease the machine')
  }
}

async function endLease(machine: Machine, mode: 'polite' | 'eager') {
  const stage = stageOf(machine)
  const running = stage?.readiness === 'busy'
  const warning =
    mode === 'eager'
      ? running
        ? '\n\nRuns in flight will be killed and their measurements lost.'
        : ''
      : running
        ? `\n\nRuns already started will finish first${
            stage?.returnable_at ? ` — expect to wait until ${exactClock(stage.returnable_at)}` : ''
          }.`
        : ''
  // What happens to production is NOT a property of the mode — it depends on
  // what was captured and on whether this deployment restores on hand-back. The
  // dialog used to promise a restore unconditionally, which was false for a
  // machine captured empty and false again wherever auto-restore is off. Ask
  // the backend, which reads it off the branch the drain will take.
  const back = stage?.hand_back
  try {
    await ElMessageBox.confirm(
      `Hand ${machine.name} back to production?${warning}\n\n` +
        (back?.summary ?? 'Check the machine before handing it back.'),
      mode === 'eager' ? 'End lease now' : 'End lease',
      {
        confirmButtonText: mode === 'eager' ? 'Stop everything' : 'End it',
        cancelButtonText: 'Cancel',
        // Leaving production down is the outcome worth a red dialog even on a
        // polite end — it is the one the operator cannot undo by waiting.
        type: mode === 'eager' || back?.owed ? 'error' : 'warning',
      },
    )
  } catch {
    return
  }
  try {
    await api.post(`/machines/${machine.name}/lease/end`, { mode, reason: 'ended from the UI' })
    const runs = mode === 'eager' ? 'Stopping runs' : 'No new runs will start'
    if (back?.owed) ElMessage.warning(`${runs}; production stays down — restore it yourself`)
    else if (back?.restores) ElMessage.success(`${runs}; production comes back after that`)
    else ElMessage.success(`${runs}; nothing of production's is affected`)
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Could not end the lease')
  }
}

function exactClock(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

const busyBaseline = ref<number | null>(null)
/** The controlled GPU-type vocabulary, from the backend so a card added there
 *  (B300, …) shows up in the dropdowns without a frontend change. */
const gpuTypes = ref<string[]>([])

/** One handler for the overflow menu: an eager hand-back sits next to the
 *  baseline overrides because both are "I know what I am doing" actions. */
function menu(machine: Machine, command: string) {
  if (command === 'end-eager') return endLease(machine, 'eager')
  if (command === 'probe') return probeCapacity(machine)
  if (command === 'edit') return openEdit(machine)
  if (command === 'remove') return removeMachine(machine)
  return baselineAction(machine, command as 'capture' | 'clear' | 'restore')
}

/** Re-read a k8s machine's GPU count and card type from the cluster. The
 *  capacity behind a node-slice drifts as nodes are added/drained/relabelled,
 *  so this is the "read it from the source of truth" button. Surfaces the
 *  probe's own warnings — a selector that matched nothing, an unknown card, a
 *  pool spanning two card types. */
async function probeCapacity(machine: Machine) {
  busyBaseline.value = machine.id
  try {
    const { data } = await api.post(`/machines/${machine.id}/probe-capacity`)
    const p = data.probe ?? {}
    const warnings: string[] = p.warnings ?? []
    const summary = `${p.gpu_count} GPU${p.gpu_count === 1 ? '' : 's'}` +
      `${p.gpu_type ? ` · ${p.gpu_type}` : ''} across ${p.node_count} node(s)`
    if (warnings.length) ElMessage.warning(`${summary} — ${warnings.join('; ')}`)
    else ElMessage.success(`Probed: ${summary}`)
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? 'Probe failed')
  } finally {
    busyBaseline.value = null
  }
}

/** The manual overrides. Named as such: the sequence runs itself, and every
 *  one of these interrupts it somewhere. Clear is the dangerous one — it stops
 *  the very service the baseline canary is queued to measure, which is how
 *  three canaries failed against a machine that was fine. */
async function baselineAction(machine: Machine, action: 'capture' | 'clear' | 'restore') {
  const stage = stageOf(machine)
  if (action === 'clear') {
    const services = machine.baseline?.services ?? []
    const warning = stage?.canary_pending
      ? `\n\nThe baseline canary has not passed yet. Stopping production now leaves ` +
        `this campaign with nothing to compare its results against.`
      : ''
    try {
      await ElMessageBox.confirm(
        `Stop ${services.length} production service(s) on ${machine.name}? ` +
          `They can be restored from the captured deploy scripts.${warning}`,
        'Clear production services',
        {
          confirmButtonText: 'Stop them',
          cancelButtonText: 'Cancel',
          type: stage?.canary_pending ? 'error' : 'warning',
        },
      )
    } catch {
      return
    }
  }
  busyBaseline.value = machine.id
  try {
    const { data } = await api.post(`/machines/${machine.id}/baseline/${action}`)
    const count = data.baseline?.services?.length ?? 0
    ElMessage.success(
      action === 'capture' ? `Captured ${count} service(s)` : `Baseline ${action}d`,
    )
    if (action === 'restore') {
      ElMessage.warning('The deploy script returns in seconds; the model loads for minutes')
    }
    await load()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? `${action} failed`)
  } finally {
    busyBaseline.value = null
  }
}

/** What the machine is to us. Driven by the lease rather than by `state`,
 *  because `available` covers both "ours for the night" and "ours but leaving",
 *  and those are the two an operator most needs to tell apart. */
function leaseLook(m: Machine): { type: string; text: string } {
  if (m.lease_state === 'draining') return { type: 'warning', text: 'handing back' }
  if (m.state === 'away' || m.lease_state === 'released' || m.lease_state === 'none') {
    return { type: 'info', text: 'with production' }
  }
  if (m.state === 'reserved') return { type: 'warning', text: 'running' }
  return { type: 'success', text: 'leased' }
}

/** What has been done to PRODUCTION on this box — a separate question from what
 *  the lease says, and one `baseline_status` cannot answer alone: `cleared`
 *  covers both "we stopped production" and "there was nothing to stop", and only
 *  the first is owed a restore. Two machines that read identically on the page
 *  while meaning opposite things is what this tag exists to end. */
function productionLook(m: Machine): { type: string; text: string; title: string } | null {
  if (m.state === 'away' && m.lease_state === 'none') return null
  const n = m.baseline?.services?.length ?? 0
  if (m.baseline_status === 'captured') {
    return {
      type: 'success',
      text: `production up · ${n} captured`,
      title: `${n} service(s) written down, still running. They can be restored from the capture.`,
    }
  }
  if (m.baseline_status === 'cleared') {
    return n
      ? {
          type: 'danger',
          text: `production down · ${n} to restore`,
          title: `We stopped ${n} production service(s) on this machine. They are owed back.`,
        }
      : {
          type: 'info',
          text: 'nothing was captured',
          title:
            'Capture reached the machine and found no production services — it was ' +
            'already free when it was handed over, so nothing is owed back.',
        }
  }
  if (m.baseline_status === 'restored') {
    return { type: 'success', text: 'production restored', title: 'Production was put back.' }
  }
  return {
    type: 'info',
    text: 'not captured yet',
    title: 'Nothing has been recorded or stopped on this machine.',
  }
}

const readinessLook: Record<string, { type: string; text: string }> = {
  busy: { type: 'warning', text: 'busy' },
  idle: { type: 'info', text: 'idle' },
  returnable: { type: 'success', text: 'returnable' },
}

const dotClass: Record<string, string> = {
  waiting: 'dot-wait',
  working: 'dot-work',
  blocked: 'dot-block',
  done: 'dot-done',
}

/** Which of the five steps are behind, at, or ahead of the machine.
 *
 *  The current step is drawn hollow while the platform is only *waiting* for
 *  it. Filling it made a machine still held by production read as already
 *  borrowed — the step it has reached and the step it is stuck before are not
 *  the same claim. */
function stepClass(machine: Machine, index: number): string {
  const stage = stageOf(machine)
  if (!stage) return ''
  if (index < stage.step) return 'past'
  if (index > stage.step) return 'future'
  if (stage.state === 'blocked') return 'now blocked'
  return stage.state === 'waiting' ? 'now pending' : 'now'
}

const anyBlocked = computed(() =>
  Object.values(lifecycle.value).some((m) => m.state === 'blocked'),
)

onMounted(async () => {
  try {
    gpuTypes.value = (await api.get('/machines/gpu-types')).data.gpu_types
  } catch {
    gpuTypes.value = []
  }
  load()
  timer = window.setInterval(load, 10000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">Resources</h1>
        <span class="muted">
          Lease a machine, schedule a campaign — the platform does the rest.
          <InfoHint :width="400">
            Per machine, automatically: <b>capture</b> what production is running,
            <b>benchmark</b> it while it is still up, <b>clear</b> it only once that
            control run passes, run the experiments, then <b>restore</b> production when
            the lease ends or the campaign's window closes.
            <br /><br />
            The same lease can be started and ended by an external fleet manager over the
            API — these buttons call exactly those endpoints.
          </InfoHint>
        </span>
      </div>
      <div>
        <el-button @click="openDocs">API</el-button>
        <el-button @click="openClusters">Clusters</el-button>
        <el-button @click="openAddGroup">Group machines</el-button>
        <el-button type="primary" @click="openAdd">Add machine</el-button>
      </div>
    </div>

    <el-alert v-if="!autoLifecycle" type="warning" :closable="false" show-icon
      style="margin-bottom: 12px"
      title="Automatic hand-over is switched off"
      description="AUTOTUNE_AUTO_BASELINE_LIFECYCLE is false, so capture, clear and
        restore have to be driven from the Override menu below. Each machine still
        shows the step it has actually reached — the switch stops the sequence
        moving, it does not make the position unknown." />

    <el-alert v-if="!autoRestore" type="info" :closable="false" show-icon
      style="margin-bottom: 12px"
      title="Ending a lease does not put production back"
      description="AUTOTUNE_AUTO_RESTORE_PRODUCTION is false, so a machine whose
        production we stopped is handed back with it still down and the restore is
        yours to do (Override ▸ Restore, which keeps working after the lease closes).
        Machines that were already free when we got them are unaffected — each card
        says which it is." />

    <div v-if="groups.length" class="groups">
      <h2 class="section-title">
        Node groups
        <InfoHint :width="420">
          A group is a named set of leased machines the platform may deploy as
          <b>one multi-node service</b> — the first member is the master. It is a
          <b>topology, not a reservation</b>: every member stays an ordinary machine
          that single-node campaigns can still use, and a member is taken only while
          a multi-node run is actually deployed on it.
          <br /><br />
          Members must share a substrate, a GPU type and a card count. Setting an
          interior address (<span class="mono">data_host</span>) on a machine is only
          needed when its engine-facing NIC differs from its management address.
        </InfoHint>
      </h2>
      <div v-for="g in groups" :key="g.id" class="card group-card">
        <div class="top">
          <div class="who">
            <span class="name">{{ g.name }}</span>
            <el-tag size="small" effect="plain">{{ g.node_count }} nodes</el-tag>
            <el-tag :type="g.deployable ? 'success' : 'warning'" size="small"
              :title="g.deployable
                ? 'Every member is leased, handed over and free.'
                : 'Advisory: the scheduler re-checks at launch.'">
              {{ g.deployable ? 'ready to deploy' : 'blocked' }}
            </el-tag>
            <span v-if="g.driver" class="muted tiny mono">{{ g.driver }}</span>
            <span v-if="g.notes" class="muted tiny">· {{ g.notes }}</span>
          </div>
          <div class="acts">
            <el-button size="small" :loading="busy && preflightGroup === g.name"
              @click="runGroupPreflight(g)">Preflight</el-button>
            <el-button size="small" @click="openEditGroup(g)">Edit</el-button>
            <el-button size="small" text class="risky" @click="removeGroup(g)">
              Dissolve
            </el-button>
          </div>
        </div>
        <div class="members">
          <div v-for="m in g.members" :key="m.name" class="member">
            <el-tag :type="m.is_master ? 'primary' : 'info'" size="small" effect="plain">
              {{ m.is_master ? 'master' : `rank ${m.rank}` }}
            </el-tag>
            <span class="mono">{{ m.name }}</span>
            <span class="muted tiny">
              {{ m.host }}<template v-if="m.data_host !== m.host"> → {{ m.data_host }}</template>
              · {{ m.gpus_busy }}/{{ m.gpu_count }} cards
            </span>
          </div>
        </div>
        <div v-if="g.blockers.length" class="blockers">
          <span v-for="b in g.blockers" :key="b" class="muted tiny">· {{ b }}</span>
        </div>
        <div v-if="g.warnings.length" class="warnings">
          <span v-for="w in g.warnings" :key="w" class="muted tiny">· {{ w }}</span>
        </div>
      </div>
    </div>

    <div v-if="!machines.length" class="empty muted">
      No machines registered yet.
    </div>

    <div v-for="m in machines" :key="m.id" class="card">
      <div class="top">
        <div class="who">
          <span class="name">{{ m.name }}</span>
          <el-tag v-if="m.driver === 'k8s'" size="small" effect="plain" type="warning">k8s</el-tag>
          <el-tag v-if="m.driver === 'k8s' && m.cluster_name" size="small" effect="plain"
            :title="`Pods land in the ${m.cluster_name} cluster`">
            {{ m.cluster_name }}
          </el-tag>
          <el-tag v-else-if="m.driver === 'k8s'" size="small" effect="plain"
            title="The platform-default cluster (AUTOTUNE_K8S_*)">
            default cluster
          </el-tag>
          <el-tag v-if="m.group" size="small" effect="plain" type="primary"
            :title="m.group_rank === 0
              ? `Master of node group ${m.group}`
              : `Worker rank ${m.group_rank} of node group ${m.group}`">
            {{ m.group }} · {{ m.group_rank === 0 ? 'master' : `rank ${m.group_rank}` }}
          </el-tag>
          <span class="mono muted">{{ m.host }}</span>
          <el-tag :type="(leaseLook(m).type as any)" size="small">{{ leaseLook(m).text }}</el-tag>
          <el-tag v-if="stageOf(m)" size="small" effect="plain"
            :type="(readinessLook[stageOf(m)!.readiness]?.type as any) ?? 'info'"
            title="What an external fleet manager is told when it asks for this machine">
            {{ readinessLook[stageOf(m)!.readiness]?.text ?? stageOf(m)!.readiness }}
          </el-tag>
          <el-tag v-if="productionLook(m)" size="small" effect="plain"
            :type="(productionLook(m)!.type as any)" :title="productionLook(m)!.title">
            {{ productionLook(m)!.text }}
          </el-tag>
          <span class="muted tiny">
            {{ m.gpus_busy }}/{{ m.gpu_count }} cards in use
            <template v-if="m.gpu_type"> · {{ m.gpu_type }}</template>
          </span>
          <span v-if="m.notes" class="muted tiny">· {{ m.notes }}</span>
        </div>

        <div class="acts">
          <el-button v-if="m.lease_state !== 'active' && m.lease_state !== 'draining'"
            size="small" type="primary" @click="lease(m)">
            Lease to platform
          </el-button>
          <el-button v-else-if="m.lease_state === 'active'" size="small"
            @click="endLease(m, 'polite')">
            End lease
          </el-button>
          <span v-else class="muted tiny">handing back…</span>

          <el-dropdown trigger="click" @command="(a: any) => menu(m, a)">
            <el-button size="small" text :loading="busyBaseline === m.id">More ▾</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="edit">
                  Edit — change fields / node selector
                </el-dropdown-item>
                <el-dropdown-item command="remove" class="risky">
                  Remove — delete this machine
                </el-dropdown-item>
                <el-dropdown-item v-if="m.driver === 'k8s'" command="probe" divided>
                  Refresh capacity — re-read GPUs from the cluster
                </el-dropdown-item>
                <el-dropdown-item command="end-eager" :divided="m.driver !== 'k8s'"
                  :disabled="m.lease_state === 'none'
                  || m.lease_state === 'released'" class="risky">
                  End lease now — stop runs immediately
                </el-dropdown-item>
                <el-dropdown-item command="capture" divided :disabled="m.state === 'away'">
                  Capture — record production now
                </el-dropdown-item>
                <el-dropdown-item command="clear"
                  :disabled="m.baseline_status !== 'captured'"
                  :class="stageOf(m)?.canary_pending ? 'risky' : ''">
                  Clear — stop production now
                </el-dropdown-item>
                <el-dropdown-item command="restore"
                  :disabled="!m.baseline?.services?.length || m.state === 'reserved'">
                  Restore — put production back
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <div v-if="m.lease_state === 'active' || m.lease_state === 'draining'" class="lease">
        <span class="muted tiny">
          leased by <b>{{ m.lease_holder || 'unknown' }}</b>
          <template v-if="m.lease_due_at"> · due back {{ relativeTime(m.lease_due_at) }}</template>
          <template v-if="stageOf(m)?.returnable_at">
            · free by {{ exactClock(stageOf(m)!.returnable_at) }}
          </template>
        </span>
      </div>

      <div class="track">
        <div v-for="(label, i) in steps" :key="label" class="step" :class="stepClass(m, i)">
          <span class="pip" />
          <span class="label">{{ label }}</span>
        </div>
      </div>

      <div class="say">
        <span class="dot" :class="dotClass[stageOf(m)?.state ?? 'waiting']" />
        <b>{{ stageOf(m)?.headline ?? '…' }}</b>
        <span class="muted">{{ stageOf(m)?.detail }}</span>
      </div>

      <div v-if="(m.lease_state === 'active' || m.lease_state === 'draining')
        && stageOf(m)?.hand_back?.summary" class="handback"
        :class="{ owed: stageOf(m)!.hand_back.owed }">
        <span class="muted tiny label">On hand-back</span>
        <span class="tiny">{{ stageOf(m)!.hand_back.summary }}</span>
      </div>

      <div v-if="stageOf(m)?.campaigns?.length" class="for">
        <span class="muted tiny">for</span>
        <el-button v-for="c in stageOf(m)!.campaigns" :key="c.id" link type="primary"
          size="small" @click="router.push(`/campaigns/${c.id}`)">
          {{ c.name || `campaign ${c.id}` }}
        </el-button>
      </div>

      <p v-if="m.baseline_status === 'cleared' && !m.baseline?.services?.length"
        class="muted tiny fold-note">
        No production services were found on this machine when it was captured, so there
        is no service list here and nothing to put back.
      </p>

      <el-collapse v-if="m.baseline?.services?.length" class="fold">
        <el-collapse-item :title="`Production on this machine (${m.baseline.services.length})`">
          <div v-for="s in m.baseline.services" :key="s.container" class="mono svc">
            {{ s.container }} · :{{ s.port }} · {{ s.served_model_name }}
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>

    <p v-if="anyBlocked" class="muted tiny foot">
      A blocked machine never has its production torn down — that is the point.
      A machine we could not measure is the one not to clear.
    </p>

    <el-dialog v-model="showAdd" :title="editingId === null ? 'Add machine' : 'Edit machine'"
      width="480px">
      <el-form label-position="top">
        <el-form-item label="Name"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="Driver">
          <el-select v-model="form.driver" style="width: 100%">
            <el-option label="SSH + Docker (bare-metal machine)" value="" />
            <el-option label="Kubernetes (a slice of the GPU cluster)" value="k8s" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.driver === 'k8s'" label="Cluster">
          <el-select v-model="form.cluster_id" clearable placeholder="platform default"
            style="width: 100%">
            <el-option v-for="c in clusters" :key="c.id"
              :label="`${c.name} · ${c.namespace} · ${c.workload_kind}`" :value="c.id" />
          </el-select>
          <div class="muted tiny">
            Which apiserver this slice's pods land in. Empty = the platform-default
            cluster (the <span class="mono">AUTOTUNE_K8S_*</span> env). Manage them under
            <strong>Clusters</strong>.
          </div>
        </el-form-item>
        <el-form-item :label="form.driver === 'k8s' ? 'Label (host is unused on k8s)' : 'Host'">
          <el-input v-model="form.host" class="mono" />
        </el-form-item>
        <div v-if="form.driver !== 'k8s'" class="grid-2">
          <el-form-item label="SSH user"><el-input v-model="form.ssh_user" /></el-form-item>
          <el-form-item label="SSH port">
            <el-input-number v-model="form.ssh_port" :min="1" :max="65535" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item :label="form.driver === 'k8s' ? 'GPU count (pool to borrow)' : 'GPU count'">
            <el-input-number v-model="form.gpu_count" :min="1" :max="64"
              :disabled="form.driver === 'k8s'" />
          </el-form-item>
          <el-form-item label="GPU type">
            <el-select v-model="form.gpu_type" clearable placeholder="(any card)"
              :disabled="form.driver === 'k8s'" style="width: 100%">
              <el-option v-for="t in gpuTypes" :key="t" :label="t" :value="t" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item v-if="form.driver === 'k8s'" label="Node selector">
          <el-input v-model="form.node_selector" class="mono"
            placeholder="kubernetes.io/hostname=gpu-a100-1" />
          <div class="muted tiny">
            Which cluster nodes this slice's pods may land on, as
            <span class="mono">label=value</span> pairs (comma-separated). Pin one node
            (<span class="mono">kubernetes.io/hostname=…</span>) or a whole card type
            (<span class="mono">nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3</span>). Needed
            when a model's weights live on only some nodes. Empty = the scheduler is free.
          </div>
        </el-form-item>
        <div v-if="form.driver === 'k8s'" class="muted tiny" style="margin: -6px 0 10px">
          GPU count and type are read from the cluster on save (and via
          <strong>Refresh capacity</strong> later) — no need to type them.
        </div>
        <template v-if="form.driver !== 'k8s'">
          <el-form-item label="Interior address (multi-node)">
            <el-input v-model="form.data_host" class="mono" placeholder="(same as host)" />
            <div class="muted tiny">
              Where the <strong>engine</strong> reaches this machine for inter-node
              traffic (NCCL / dist-init). Leave empty unless its rail NIC differs from the
              management address above — which is also the address LLMBench is given and
              the one ssh uses. Only matters inside a node group.
            </div>
          </el-form-item>
          <el-form-item label="NCCL interface">
            <el-input v-model="form.nccl_ifname" class="mono" placeholder="(engine default)" />
            <div class="muted tiny">
              <span class="mono">NCCL_SOCKET_IFNAME</span>, when the routable IP and the
              InfiniBand/RoCE rail differ. Usually set on the group instead.
            </div>
          </el-form-item>
        </template>
        <el-form-item label="Notes"><el-input v-model="form.notes" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showAdd = false">Cancel</el-button>
        <el-button type="primary" :loading="busy" @click="save">
          {{ editingId === null ? 'Add' : 'Save' }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showGroup" width="560px"
      :title="editingGroupId === null ? 'Group machines for multi-node' : 'Edit node group'">
      <el-form label-position="top">
        <el-form-item label="Name">
          <el-input v-model="groupForm.name" :disabled="editingGroupId !== null"
            placeholder="e.g. nv-pair-a" />
          <div v-if="editingGroupId !== null" class="muted tiny">
            A name is permanent — campaigns pin the group by it.
          </div>
        </el-form-item>
        <el-form-item label="Members — selection order is the rank order, first is master">
          <el-select v-model="groupForm.members" multiple style="width: 100%"
            placeholder="Pick the machines this group may deploy across">
            <el-option v-for="c in memberChoices" :key="c.name" :label="c.label"
              :value="c.name" :disabled="c.disabled" />
          </el-select>
          <div class="muted tiny">
            All members must share a substrate, a GPU type and a card count. A machine
            already in another group is greyed out.
          </div>
        </el-form-item>
        <div class="grid-2">
          <el-form-item label="Substrate">
            <el-select v-model="groupForm.driver" style="width: 100%">
              <el-option label="(each machine's own)" value="" />
              <el-option label="SSH + Docker" value="ssh_docker" />
              <el-option label="Kubernetes" value="k8s" />
            </el-select>
          </el-form-item>
          <el-form-item label="Rendezvous port">
            <el-input-number v-model="groupForm.dist_port" :min="0" :max="65535" />
            <div class="muted tiny">0 = pick a free port per run on the master.</div>
          </el-form-item>
        </div>
        <el-form-item label="NCCL environment">
          <el-input v-model="groupForm.ncclEnvText" type="textarea" :rows="3" class="mono"
            placeholder="NCCL_SOCKET_IFNAME=ib0" />
          <div class="muted tiny">
            One <span class="mono">KEY=VALUE</span> per line, applied to every rank's
            container — one fact about one fabric.
          </div>
        </el-form-item>
        <el-form-item label="Notes"><el-input v-model="groupForm.notes" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showGroup = false">Cancel</el-button>
        <el-button type="primary" :loading="busy"
          :disabled="!groupForm.name.trim() || !groupForm.members.length" @click="saveGroup">
          {{ editingGroupId === null ? 'Create group' : 'Save' }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showGroupPreflight" :title="`Preflight · ${preflightGroup}`" width="640px">
      <div v-if="!groupPreflight" class="muted">
        Probing every member and the interior links between them…
      </div>
      <template v-else>
        <el-alert v-if="groupPreflight.note" type="info" :closable="false"
          :title="groupPreflight.note" style="margin-bottom: 12px" />
        <el-alert v-if="groupPreflight.ok" type="success" :closable="false" show-icon
          title="Every member is ready and can reach the master"
          description="Advisory only: the scheduler re-checks at launch."
          style="margin-bottom: 12px" />
        <el-alert v-else-if="!groupPreflight.note" type="error" :closable="false" show-icon
          title="Something would stop this group deploying"
          description="These are cheap checks for expensive failures — fix them before a run
            spends its window discovering them." style="margin-bottom: 12px" />
        <div v-for="row in groupPreflight.machines" :key="row.machine" class="pf-row">
          <div class="pf-head">
            <span class="mono">{{ row.machine }}</span>
            <el-tag size="small" :type="row.ok ? 'success' : 'danger'" effect="plain">
              {{ row.ok ? 'ok' : `${row.failed} failed` }}
            </el-tag>
          </div>
          <div v-for="c in row.checks" :key="row.machine + c.key" class="pf-check">
            <el-tag size="small" :type="(checkTag[c.status] as any)" effect="plain">
              {{ c.status }}
            </el-tag>
            <span class="pf-label">{{ c.label }}</span>
            <span class="muted tiny">{{ c.detail }}</span>
          </div>
        </div>
      </template>
      <template #footer>
        <el-button @click="showGroupPreflight = false">Close</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showClusters" title="Kubernetes clusters" width="680px">
      <p class="muted tiny" style="margin-top: -6px">
        A k8s machine lands in one of these. The kubeconfig is stored encrypted
        and never shown again — leave the box empty when editing to keep it.
      </p>
      <div v-for="c in clusters" :key="c.id" class="card" style="margin-bottom: 8px">
        <div class="top">
          <div class="who">
            <span class="name">{{ c.name }}</span>
            <el-tag size="small" effect="plain">{{ c.namespace }}</el-tag>
            <el-tag size="small" effect="plain"
              :type="c.workload_kind === 'custom' ? 'warning' : 'info'">
              {{ c.workload_kind }}
            </el-tag>
            <el-tag size="small" effect="plain" type="info">{{ c.api_mode }}</el-tag>
            <el-tag v-if="c.machine_count" size="small" effect="plain" type="success">
              {{ c.machine_count }} machine(s)
            </el-tag>
            <el-tag v-if="!c.has_kubeconfig" size="small" effect="plain" type="danger">
              no credential
            </el-tag>
          </div>
          <div class="acts">
            <el-button size="small" :loading="probingCluster === c.id"
              @click="probeCluster(c)">Probe</el-button>
            <el-button size="small" @click="openEditCluster(c)">Edit</el-button>
            <el-button size="small" text class="risky" @click="removeCluster(c)">
              Remove
            </el-button>
          </div>
        </div>
        <div class="muted tiny mono">
          {{ c.gpu_resource
          }}<template v-if="c.node_host"> · endpoint {{ c.node_host }}</template
          ><template v-if="c.runtime_class"> · runtimeClass {{ c.runtime_class }}</template>
          · shm {{ c.shm_size_mb }}Mi
        </div>
        <div v-if="c.last_probe?.warnings?.length" class="warnings">
          <span v-for="w in c.last_probe.warnings" :key="w" class="muted tiny">· {{ w }}</span>
        </div>
        <div v-else-if="c.last_probe_at" class="muted tiny">
          probed {{ relativeTime(c.last_probe_at) }}
        </div>
      </div>
      <div v-if="!clusters.length" class="empty muted">
        No clusters yet — the platform-default cluster is still served from
        <span class="mono">AUTOTUNE_K8S_*</span>.
      </div>

      <el-divider />
      <h3 class="muted">
        {{ editingClusterId === null ? 'Add a cluster' : `Edit ${clusterForm.name}` }}
      </h3>
      <el-form label-position="top">
        <div class="grid-2">
          <el-form-item label="Name">
            <el-input v-model="clusterForm.name" placeholder="gpu-cluster-a" />
          </el-form-item>
          <el-form-item label="Namespace">
            <el-input v-model="clusterForm.namespace" class="mono" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item label="API mode">
            <el-select v-model="clusterForm.api_mode" style="width: 100%">
              <el-option label="client (Python client)" value="client" />
              <el-option label="kubectl (shell out)" value="kubectl" />
              <el-option label="unavailable" value="unavailable" />
            </el-select>
          </el-form-item>
          <el-form-item label="Workload kind">
            <el-select v-model="clusterForm.workload_kind" style="width: 100%">
              <el-option label="deployment (no operator)" value="deployment" />
              <el-option label="custom (TuningRun CRD)" value="custom" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="Kubeconfig (scoped, never an admin one)">
          <el-input v-model="clusterForm.kubeconfig" type="textarea" :rows="5" class="mono"
            :placeholder="editingClusterId === null
              ? 'paste the kubeconfig YAML'
              : 'leave empty to keep the stored credential'" />
        </el-form-item>
        <div class="grid-2">
          <el-form-item label="Endpoint host (node_host)">
            <el-input v-model="clusterForm.node_host" class="mono" placeholder="198.51.100.10" />
          </el-form-item>
          <el-form-item label="RuntimeClass">
            <el-input v-model="clusterForm.runtime_class" class="mono" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item label="GPU resource">
            <el-input v-model="clusterForm.gpu_resource" class="mono" />
          </el-form-item>
          <el-form-item label="/dev/shm (MiB)">
            <el-input-number v-model="clusterForm.shm_size_mb" :min="0" :max="1048576" />
          </el-form-item>
        </div>
        <el-form-item label="Default node selector">
          <el-input v-model="clusterForm.node_selector" class="mono" placeholder="(none)" />
        </el-form-item>
        <el-form-item label="Extra tolerations">
          <el-input v-model="clusterForm.tolerations" class="mono"
            placeholder="e.g. node.kubernetes.io/unschedulable" />
          <div class="muted tiny">
            Comma-separated <span class="mono">key[=value][:effect]</span>. Needed for a
            <strong>cordoned</strong> pool: a pod that tolerates
            <span class="mono">node.kubernetes.io/unschedulable</span> still schedules
            onto it, where one that does not sits Pending forever. The GPU taint is
            handled automatically when “tolerate GPU taint” is on.
          </div>
        </el-form-item>
        <el-form-item label="Image pull secrets">
          <el-input v-model="clusterForm.image_pull_secrets" class="mono"
            placeholder="(none — nodes already carry registry creds)" />
        </el-form-item>
        <div class="grid-2">
          <el-form-item label="Engine cpu request">
            <el-input v-model="clusterForm.engine_cpu_request" class="mono" placeholder="(omit)" />
          </el-form-item>
          <el-form-item label="Engine memory request">
            <el-input v-model="clusterForm.engine_memory_request" class="mono" placeholder="(omit)" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item label="Engine cpu limit">
            <el-input v-model="clusterForm.engine_cpu_limit" class="mono" placeholder="(omit)" />
          </el-form-item>
          <el-form-item label="Engine memory limit">
            <el-input v-model="clusterForm.engine_memory_limit" class="mono" placeholder="(omit)" />
          </el-form-item>
        </div>
        <el-form-item label="Notes">
          <el-input v-model="clusterForm.notes" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showClusters = false">Close</el-button>
        <el-button type="primary" :loading="busy" @click="saveCluster">
          {{ editingClusterId === null ? 'Add cluster' : 'Save' }}
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
  margin-bottom: 12px;
  gap: 24px;
}
.card {
  background: #fff;
  border: 1px solid var(--autotune-border);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 12px;
}
/* Node groups sit above the machine list because they are a coarser view of
   the same fleet — and the page must keep showing the members underneath. */
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  margin: 18px 0 10px;
}
.members {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
}
.member {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.blockers {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 8px;
  color: var(--el-color-warning);
}
.warnings {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 6px;
  color: var(--el-color-info);
}
.pf-row {
  padding: 8px 0;
  border-top: 1px solid var(--autotune-border);
}
.pf-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.pf-check {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 2px 0;
}
.pf-label {
  font-size: 12.5px;
  min-width: 130px;
}
.top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.who {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.name {
  font-weight: 600;
  font-size: 15px;
}
.acts {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tiny {
  font-size: 11.5px;
}

/* The sequence as a position rather than a paragraph. */
.track {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 14px 0 10px;
}
.step {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}
.step .pip {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex: none;
  background: var(--autotune-border);
}
.step .label {
  font-size: 11.5px;
  color: var(--autotune-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.step::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--autotune-border);
  min-width: 8px;
}
.step:last-child::after {
  display: none;
}
.step.past .pip {
  background: var(--el-color-primary-light-3);
}
.step.past .label {
  color: var(--autotune-text);
}
.step.now .pip {
  background: var(--el-color-primary);
  box-shadow: 0 0 0 3px var(--el-color-primary-light-8);
}
.step.now .label {
  color: var(--el-color-primary-dark-2);
  font-weight: 600;
}
.step.now.pending .pip {
  background: #fff;
  border: 2px solid var(--el-color-primary);
  box-shadow: none;
}
.step.now.pending .label {
  font-weight: 400;
}
.step.blocked .pip {
  background: var(--el-color-danger);
  box-shadow: 0 0 0 3px #fde2e2;
}
.step.blocked .label {
  color: var(--el-color-danger);
}

.say {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  flex-wrap: wrap;
}
.say .muted {
  flex: 1;
  min-width: 240px;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
  align-self: center;
}
.dot-wait {
  background: var(--autotune-muted);
}
.dot-work {
  background: var(--el-color-primary);
}
.dot-block {
  background: var(--el-color-danger);
}
.dot-done {
  background: var(--el-color-success);
}
.handback {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 6px;
  padding: 5px 8px;
  border-left: 2px solid var(--el-border-color);
  background: var(--el-fill-color-lighter);
  border-radius: 0 3px 3px 0;
}
.handback .label {
  flex: none;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
/* Production left down is the outcome an operator cannot undo by waiting, so
   it is the one that gets colour. */
.handback.owed {
  border-left-color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}
.fold-note {
  margin: 6px 0 0;
}
.for {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}
.fold {
  margin-top: 6px;
  border-top: none;
}
.fold :deep(.el-collapse-item__header) {
  font-size: 12px;
  height: 32px;
  line-height: 32px;
  border-bottom: none;
}
.fold :deep(.el-collapse-item__wrap) {
  border-bottom: none;
}
.svc {
  line-height: 1.7;
}
.empty {
  padding: 24px;
  text-align: center;
}
.foot {
  margin-top: 4px;
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
:deep(.risky) {
  color: var(--el-color-danger);
}
</style>
