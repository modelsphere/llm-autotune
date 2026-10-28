<script setup lang="ts">
import { apiErrorText } from '../utils/apiError'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, type Cluster, type Machine, type MachineGroup, type MachineGroupPreflight, type MachineLifecycle } from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import ClusterEndpointFields from '../components/ClusterEndpointFields.vue'
import ClusterHealth from '../components/ClusterHealth.vue'
import LiveWorkDialog, { type LiveRun } from '../components/LiveWorkDialog.vue'
import SmokeTestDialog from '../components/SmokeTestDialog.vue'
import { Lock, Warning } from '@element-plus/icons-vue'
import { readKubeconfig } from '../utils/kubeconfig'
import PageLoading from '../components/PageLoading.vue'
import LinkButton from '../components/LinkButton.vue'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/auth'
import { all, useFirstLoad } from '../utils/load'
import { usePoll } from '../utils/poll'
import { relativeTime } from '../utils/time'

/** Adding, editing and probing clusters is for admins; the API refuses the
 *  rest with a 403, so the buttons are not offered to them at all. */
const { t } = useI18n()
const auth = useAuthStore()
const isAdmin = computed(() => auth.user?.role === 'admin')

/** Machines and the clusters they land in, one tab each; the tab rides in the
 *  URL so a link or a reload keeps it. */
const route = useRoute()
const router = useRouter()
const tab = ref<'machines' | 'clusters'>(route.query.tab === 'clusters' ? 'clusters' : 'machines')
watch(tab, (value) => {
  router.replace({ query: { ...route.query, tab: value === 'machines' ? undefined : value } })
})

/** Reaching `window` from a template needs it bound explicitly under
 *  `<script setup>`; the component instance is not the global object. */
const openDocs = () => window.open('/api/docs', '_blank')
/** Where giving the platform access to a machine is written down. */
const setupDocs = 'https://github.com/modelsphere/llm-autotune/blob/main/docs/after-installing.md#gpus'

const machines = ref<Machine[]>([])
const groups = ref<MachineGroup[]>([])
/** The Kubernetes clusters a k8s machine may land in. `cluster_id === null` on
 *  a machine means the platform-default cluster, which has no row here. */
const clusters = ref<Cluster[]>([])
const steps = ref<string[]>([])
const lifecycle = ref<Record<number, MachineLifecycle>>({})
const showAdd = ref(false)
const addAdvanced = ref<string[]>([])
const busy = ref(false)
const poll = usePoll(() => load(), 10000)

const emptyForm = () => ({
  name: '',
  host: '',
  ssh_user: 'root',
  ssh_port: 22,
  /** null: read from the machine on save. */
  gpu_count: null as number | null,
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

/** A launch kind as a tag: LLMService is current, the rest are deprecated. */
function launchLook(kind: string): { text: string; type: 'success' | 'warning' } {
  const text = ({
    llmservice: 'LLMService',
    deployment: 'Deployment',
    custom: 'TuningRun',
    ssh_docker: 'SSH + Docker',
  } as Record<string, string>)[kind] ?? kind
  return kind === 'llmservice'
    ? { text, type: 'success' }
    : { text: `${text} ${t('resources.wkDeprecated')}`, type: 'warning' }
}

/** What the machine dialog would launch with, as it is filled in now. null =
 *  the platform-default cluster, whose kind only the API knows. */
const formLaunchKind = computed<string | null>(() => {
  if (form.value.driver !== 'k8s') return 'ssh_docker'
  if (form.value.cluster_id !== null) {
    return clusters.value.find((c) => c.id === form.value.cluster_id)?.workload_kind ?? null
  }
  const editing = machines.value.find((m) => m.id === editingId.value)
  return editing && editing.driver === 'k8s' && editing.cluster_id === null ? editing.launch_kind : null
})

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
/** An edit refused because of live work: what is still running, to tell busy
 *  from stuck. */
const liveWork = ref<{ open: boolean; message: string; runs: LiveRun[]; sessions: LiveRun[] }>(
  { open: false, message: '', runs: [], sessions: [] })
const editingClusterId = ref<number | null>(null)
const emptyClusterForm = () => ({
  name: '',
  api_mode: 'client',
  kubeconfig: '',
  namespace: '',
  workload_kind: 'llmservice',
  chart_repo: '',
  chart_credentials_secret: '',
  endpoint: 'dns',
  image_mirror: '',
  site_values: '',
  node_host: '',
  service_nodeport: 0,
  gpu_resource: 'nvidia.com/gpu',
  gpu_product_label: 'nvidia.com/gpu.product',
  runtime_class: 'nvidia',
  shm_size_mb: 2048,
  node_selector: '',
  policy_node_selector: '',
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

/** -- adding a GPU cluster ---------------------------------------------------
 *
 * Upload the kubeconfig deploy/gpu-cluster.sh wrote (it names its namespace),
 * then register its GPUs: one pool per GPU type (a machine spanning every node
 * of that type, runs going wherever the cluster has room), or one machine
 * pinned to each node. The step also re-opens from a cluster's Nodes button,
 * to register nodes added since or see ones that left.
 */
interface GpuNode {
  node: string
  hostname: string
  gpu_count: number
  gpu_type: string
  gpu_product: string
  ready: boolean
  schedulable: boolean
  /** Ready and either not cordoned or cordoned with the cordon tolerated. */
  takes_pods: boolean
  machine: { id: number; name: string } | null
  /** The registered pool it is already part of. */
  pool: { id: number; name: string } | null
}
interface GpuPool {
  product: string
  gpu_type: string
  nodes: string[]
  /** Cards on nodes that take pods: the pool's capacity. */
  gpu_count: number
  gpu_total: number
  nodes_usable: number
  node_gpu_max: number
  machine: { id: number; name: string } | null
  /** Its nodes already registered on their own. */
  single_nodes: string[]
}
interface NodesView {
  cluster: string
  namespace: string
  nodes_readable: boolean
  nodes: GpuNode[]
  pools: GpuPool[]
  gone: { id: number; name: string; hostname: string }[]
  /** No node access: the namespace's GPU quota, if readable. */
  quota: { gpus: number; used: number } | null
  namespace_machine: { id: number; name: string } | null
}
const showAddCluster = ref(false)
const addStep = ref<'connect' | 'nodes'>('connect')
const clusterAdvanced = ref<string[]>([])
const emptyNewCluster = () => ({
  name: '', kubeconfig: '', namespace: '', workload_kind: 'llmservice', endpoint: 'dns',
  node_host: '', service_nodeport: 0,
  runtime_class: 'nvidia', gpu_resource: 'nvidia.com/gpu',
  gpu_product_label: 'nvidia.com/gpu.product', tolerations: '',
  image_pull_secrets: '', node_selector: '', policy_node_selector: '',
  chart_repo: '', image_mirror: '',
})
const newCluster = ref(emptyNewCluster())
/** A failed Connect: the probe that says why, nothing saved. */
const connectFailure = ref<{ message: string; probe: Record<string, any> } | null>(null)
const nodesCluster = ref<{ id: number; name: string } | null>(null)
const nodesView = ref<NodesView | null>(null)
const nodesError = ref('')
const pickedNodes = ref<string[]>([])
const pickedPools = ref<string[]>([])
/** Pools by default: on a shared cluster, the scheduler is the one who knows
 *  which node has room. One machine per node is for pinning work by hand. */
const registerAs = ref<'pools' | 'nodes'>('pools')
const picked = computed(() =>
  registerAs.value === 'pools' ? pickedPools.value.length : pickedNodes.value.length)
/** Lease the nodes as they are registered: one step instead of a Lease press
 *  per machine. */
const leaseOnRegister = ref(true)
const kubeconfigFile = ref<HTMLInputElement | null>(null)

/** Read from the pasted kubeconfig: the name to suggest until one is typed,
 *  and whether it names a namespace (if not, the field leaves Advanced). */
const kubeFacts = computed(() => readKubeconfig(newCluster.value.kubeconfig))
watch(() => kubeFacts.value.name, (name, before) => {
  if (!newCluster.value.name || newCluster.value.name === (before ?? '')) {
    newCluster.value.name = name
  }
})
const namespaceMissing = computed(() =>
  !!newCluster.value.kubeconfig.trim() && !kubeFacts.value.namespace)

function openAddCluster() {
  newCluster.value = emptyNewCluster()
  connectFailure.value = null
  clusterAdvanced.value = []
  addStep.value = 'connect'
  nodesView.value = null
  nodesError.value = ''
  showAddCluster.value = true
}

async function readKubeconfigFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (file) newCluster.value.kubeconfig = await file.text()
  ;(event.target as HTMLInputElement).value = ''
}

async function loadNodes(cluster: { id: number; name: string }) {
  nodesCluster.value = cluster
  nodesView.value = null
  nodesError.value = ''
  try {
    nodesView.value = (await api.get(`/clusters/${cluster.id}/nodes`)).data
    // Ticked by default: what can be registered and would hold work now.
    pickedNodes.value = nodesView.value!.nodes
      .filter((n) => !n.machine && !n.pool && n.takes_pods).map((n) => n.hostname)
    pickedPools.value = nodesView.value!.pools
      .filter((p) => !p.machine && !p.single_nodes.length && p.gpu_count > 0)
      .map((p) => p.product)
  } catch (error: any) {
    nodesError.value = apiErrorText(error, t('resources.err.nodes'))
  }
}

async function connectCluster() {
  busy.value = true
  connectFailure.value = null
  try {
    // check: probed before it is kept, so an unreachable one is never saved.
    const { data } = await api.post('/clusters?check=true', newCluster.value)
    await load()
    addStep.value = 'nodes'
    await loadNodes(data)
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    if (detail?.probe) connectFailure.value = { message: detail.message, probe: detail.probe }
    else ElMessage.error(apiErrorText(error, t('resources.err.addCluster')))
  } finally {
    busy.value = false
  }
}

function openNodes(c: Cluster) {
  showClusters.value = false
  addStep.value = 'nodes'
  showAddCluster.value = true
  loadNodes(c)
}

async function registerNodes() {
  if (!nodesCluster.value) return
  busy.value = true
  try {
    const { data } = registerAs.value === 'pools'
      ? await api.post(`/clusters/${nodesCluster.value.id}/pools`,
        { products: pickedPools.value })
      : await api.post(`/clusters/${nodesCluster.value.id}/nodes`,
        { nodes: pickedNodes.value })
    await afterRegister(data.registered)
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('resources.err.register')))
  } finally {
    busy.value = false
  }
}

/** Lease what was just registered (the same call as the Lease button, so the
 *  machines end up identical), saying apart what registered and what did not
 *  lease. */
async function afterRegister(names: string[]) {
  const failed: string[] = []
  let reason = ''
  if (leaseOnRegister.value) {
    for (const name of names) {
      try {
        await api.post('/machines/lease', { name, lease_note: 'leased when registered' })
      } catch (error: any) {
        failed.push(name)
        reason = apiErrorText(error, t('resources.err.register'))
      }
    }
  }
  if (failed.length) {
    ElMessage.warning(t('resources.wizard.leaseFailed', { names: failed.join(', '), reason }))
  } else {
    ElMessage.success(names.length
      ? (leaseOnRegister.value ? t('resources.msg.registeredLeased', { names: names.join(', ') }) : t('resources.msg.registered', { names: names.join(', ') }))
      : t('resources.msg.nothingNew'))
  }
  showAddCluster.value = false
  await load()
}

/** No node access: the namespace's quota as one pool. */
async function registerNamespacePool() {
  if (!nodesCluster.value) return
  busy.value = true
  try {
    const { data } = await api.post(`/clusters/${nodesCluster.value.id}/pools`, { namespace: true })
    await afterRegister(data.registered)
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('resources.err.register')))
  } finally {
    busy.value = false
  }
}

/** The cluster row behind the Register step: its probe and tolerations. */
const nodesClusterRow = computed(() =>
  clusters.value.find((c) => c.id === nodesCluster.value?.id) ?? null)
const CORDON = 'node.kubernetes.io/unschedulable'
const tolerated = computed(() =>
  (nodesClusterRow.value?.tolerations ?? '').split(',').some((x) => x.trim().startsWith(CORDON)))
const cordonedNodes = computed(() =>
  (nodesView.value?.nodes ?? []).filter((n) => n.ready && !n.schedulable))

/** Cordoned nodes an admin set aside for us: tolerate the cordon on this
 *  cluster, then read the nodes again so they count. */
async function useCordoned() {
  const row = nodesClusterRow.value
  if (!row) return
  busy.value = true
  try {
    const form = clusterFormOf(row)
    form.tolerations = [form.tolerations, `${CORDON}:NoSchedule`].filter((x) => x.trim()).join(',')
    await api.put(`/clusters/${row.id}`, form)
    await load()
    await loadNodes(row)
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('resources.err.save')))
  } finally {
    busy.value = false
  }
}

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
    ElMessage.error(apiErrorText(error, t('resources.err.preflight')))
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
      m.lease_state === 'active' ? t('resources.look.leased') : m.state
    }${m.group ? ` · ${t('resources.inGroup', { group: m.group })}` : ''}`,
    disabled: Boolean(m.group) && m.group !== groupForm.value.name,
  })),
)

async function load() {
  const [ms, gs, cs, { data }] = await all(
    api.get('/machines'),
    // The fleet list is the page's job; groups and clusters are extras on it.
    api.get('/machine-groups').catch(() => null),
    api.get('/clusters').catch(() => null),
    api.get('/machines/lifecycle'),
  )
  machines.value = ms.data
  groups.value = gs?.data ?? []
  clusters.value = cs?.data ?? []
  steps.value = data.steps
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
  // A cluster when there is one: SSH + Docker is the deprecated way.
  const cluster = clusters.value.find((c) => c.workload_kind === 'llmservice') ?? clusters.value[0]
  if (cluster) {
    form.value.driver = 'k8s'
    form.value.cluster_id = cluster.id
  }
  leaseOnAdd.value = true
  showAdd.value = true
}
/** Lease the machine as it is added, as registering from a cluster does. */
const leaseOnAdd = ref(true)

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
    ElMessage.error(apiErrorText(error, t('resources.err.save')))
  } finally {
    busy.value = false
  }
}

async function removeGroup(group: MachineGroup) {
  try {
    await ElMessageBox.confirm(
      t('resources.dissolveConfirm', { name: group.name }),
      t('resources.dissolveTitle'),
      { confirmButtonText: t('resources.dissolve'), cancelButtonText: t('common.cancel'), type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/machine-groups/${group.id}`)
    ElMessage.success(t('resources.msg.dissolved', { name: group.name }))
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('resources.err.dissolve')))
  }
}


function openEditCluster(c: Cluster) {
  editingClusterId.value = c.id
  clusterForm.value = clusterFormOf(c)
  showClusters.value = true
}

/** A cluster as the edit form holds it. The kubeconfig stays EMPTY: the API
 *  never returns it, and empty on save means "keep the stored credential". */
function clusterFormOf(c: Cluster) {
  return {
    name: c.name,
    api_mode: c.api_mode,
    kubeconfig: '',
    namespace: c.namespace,
    workload_kind: c.workload_kind,
    chart_repo: c.chart_repo ?? '',
    chart_credentials_secret: c.chart_credentials_secret ?? '',
    endpoint: c.endpoint ?? 'dns',
    image_mirror: c.image_mirror ?? '',
    site_values: c.site_values ?? '',
    node_host: c.node_host,
    service_nodeport: c.service_nodeport,
    gpu_resource: c.gpu_resource,
    gpu_product_label: c.gpu_product_label,
    runtime_class: c.runtime_class,
    shm_size_mb: c.shm_size_mb,
    node_selector: c.node_selector,
    policy_node_selector: c.policy_node_selector ?? '',
    tolerations: c.tolerations,
    image_pull_secrets: c.image_pull_secrets,
    engine_cpu_request: c.engine_cpu_request,
    engine_memory_request: c.engine_memory_request,
    engine_cpu_limit: c.engine_cpu_limit,
    engine_memory_limit: c.engine_memory_limit,
    notes: c.notes,
  }
}

async function saveCluster() {
  busy.value = true
  try {
    const payload = { ...clusterForm.value }
    if (editingClusterId.value === null) await api.post('/clusters', payload)
    else await api.put(`/clusters/${editingClusterId.value}`, payload)
    ElMessage.success(t('resources.msg.saved', { name: payload.name }))
    showClusters.value = false
    editingClusterId.value = null
    clusterForm.value = emptyClusterForm()
    await load()
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    if (detail?.runs || detail?.sessions) {
      liveWork.value = { open: true, message: detail.message,
                         runs: detail.runs ?? [], sessions: detail.sessions ?? [] }
    } else {
      ElMessage.error(apiErrorText(error, t('resources.err.save')))
    }
  } finally {
    busy.value = false
  }
}

async function removeCluster(c: Cluster) {
  try {
    await ElMessageBox.confirm(
      t('resources.removeClusterConfirm', { name: c.name }),
      t('resources.removeClusterTitle'),
      { confirmButtonText: t('resources.remove'), cancelButtonText: t('common.cancel'), type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/clusters/${c.id}`)
    ElMessage.success(t('resources.msg.removed', { name: c.name }))
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('resources.err.removeCluster')))
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
      ? t('resources.reachable', { n: probe.node_count })
      : t('resources.unreachable')
    if (warnings.length) ElMessage.warning(`${c.name}: ${summary} — ${warnings.join('; ')}`)
    else ElMessage.success(`${c.name}: ${summary}`)
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('resources.err.probe')))
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
    if (editingId.value === null) {
      await api.post('/machines', payload)
      if (leaseOnAdd.value) {
        await api.post('/machines/lease', { name: payload.name, lease_note: 'leased when added' })
      }
    } else await api.put(`/machines/${editingId.value}`, payload)
    showAdd.value = false
    await load()
  } catch (error: any) {
    const text = apiErrorText(error, t('resources.err.save'))
    // The machine could not be read: the count it needs is under Advanced.
    if (text.includes('GPU count')) addAdvanced.value = ['advanced']
    ElMessage.error(text)
  } finally {
    busy.value = false
  }
}

async function removeMachine(machine: Machine) {
  try {
    await ElMessageBox.confirm(
      t('resources.removeMachineConfirm', { name: machine.name }),
      t('resources.removeMachineTitle'),
      { confirmButtonText: t('resources.remove'), cancelButtonText: t('common.cancel'), type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.delete(`/machines/${machine.id}`)
    ElMessage.success(t('resources.msg.removed', { name: machine.name }))
    await load()
  } catch (error: any) {
    // 409 carries the reason (campaigns pinned / live run) — show it verbatim.
    ElMessage.error(apiErrorText(error, t('resources.err.remove')))
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
    ElMessage.error(apiErrorText(error, t('resources.err.lease')))
  }
}

async function endLease(machine: Machine, mode: 'polite' | 'eager') {
  const stage = stageOf(machine)
  const running = stage?.readiness === 'busy'
  const warning =
    mode === 'eager'
      ? running
        ? '\n\n' + t('resources.end.killed')
        : ''
      : running
        ? '\n\n' + (stage?.returnable_at
            ? t('resources.end.finishUntil', { time: exactClock(stage.returnable_at) })
            : t('resources.end.finish'))
        : ''
  try {
    await ElMessageBox.confirm(
      t('resources.end.confirm', { name: machine.name, warning }),
      mode === 'eager' ? t('resources.endLeaseNow') : t('resources.endLease'),
      {
        confirmButtonText: mode === 'eager' ? t('resources.end.stopAll') : t('resources.end.endIt'),
        cancelButtonText: t('common.cancel'),
        type: mode === 'eager' ? 'error' : 'warning',
      },
    )
  } catch {
    return
  }
  try {
    await api.post(`/machines/${machine.name}/lease/end`, { mode, reason: 'ended from the UI' })
    ElMessage.success(mode === 'eager' ? t('resources.msg.stopping') : t('resources.msg.noNew'))
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('resources.err.endLease')))
  }
}

function exactClock(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

const busyMachine = ref<number | null>(null)
/** The controlled GPU-type vocabulary, from the backend so a card added there
 *  (B300, …) shows up in the dropdowns without a frontend change. */
const gpuTypes = ref<string[]>([])

/** One handler for the overflow menu. */
function menu(machine: Machine, command: string) {
  if (command === 'end-eager') return endLease(machine, 'eager')
  if (command === 'probe') return probeCapacity(machine)
  if (command === 'edit') return openEdit(machine)
  if (command === 'remove') return removeMachine(machine)
}

/** The machine smoke test (SmokeTestDialog runs it and shows its steps). */
const smokeMachine = ref<Machine | null>(null)
const showSmoke = ref(false)
function openSmoke(machine: Machine) {
  smokeMachine.value = machine
  showSmoke.value = true
}

/** Re-read a machine's GPU count and card type from the machine itself
 *  (nvidia-smi, or the k8s nodes its selector matches) — capacity drifts as
 *  nodes are added, drained and re-carded. Surfaces the
 *  probe's own warnings — a selector that matched nothing, an unknown card, a
 *  pool spanning two card types. */
async function probeCapacity(machine: Machine) {
  busyMachine.value = machine.id
  try {
    const { data } = await api.post(`/machines/${machine.id}/probe-capacity`)
    const p = data.probe ?? {}
    const warnings: string[] = p.warnings ?? []
    const summary = t('resources.probed', { n: p.gpu_count, type: p.gpu_type ? ` · ${p.gpu_type}` : '', nodes: p.node_count })
    if (warnings.length) ElMessage.warning(`${summary} — ${warnings.join('; ')}`)
    else ElMessage.success(t('resources.msg.probed', { summary }))
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('resources.err.probe')))
  } finally {
    busyMachine.value = null
  }
}

/** What the machine is to us. Driven by the lease rather than by `state`,
 *  because `available` covers both "ours for the night" and "ours but leaving",
 *  and those are the two an operator most needs to tell apart. */
function leaseLook(m: Machine): { type: string; text: string } {
  if (m.lease_state === 'draining') return { type: 'warning', text: t('resources.look.handingBack') }
  if (m.state === 'away' || m.lease_state === 'released' || m.lease_state === 'none') {
    return { type: 'info', text: t('resources.look.notLeased') }
  }
  if (m.state === 'reserved') return { type: 'warning', text: t('resources.look.running') }
  return { type: 'success', text: t('resources.look.leased') }
}

const readinessLook = computed<Record<string, { type: string; text: string }>>(() => ({
  busy: { type: 'warning', text: t('resources.look.busy') },
  idle: { type: 'info', text: t('resources.look.idle') },
  returnable: { type: 'success', text: t('resources.look.returnable') },
}))

/** The lifecycle's step names come from the API in English; known ones are
 *  shown in the page's language. */
const STEP_KEYS: Record<string, string> = { Leased: 'leased', 'Running campaigns': 'running', 'Handed back': 'handedBack' }
const stepLabel = (label: string) => (STEP_KEYS[label] ? t(`resources.steps.${STEP_KEYS[label]}`) : label)

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

const first = useFirstLoad(load)
onMounted(() => {
  void first.run()
  api.get('/machines/gpu-types')
    .then(({ data }) => (gpuTypes.value = data.gpu_types))
    .catch(() => (gpuTypes.value = []))
  poll.start()
})
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">{{ t('resources.title') }}
          <InfoHint :width="400"><span v-html="t('resources.hint')" />
          </InfoHint>
        </h1>
      </div>
      <div v-if="tab === 'machines'">
        <el-button text @click="openDocs">API</el-button>
        <template v-if="isAdmin">
          <el-button @click="openAddGroup">{{ t('resources.groupMachines') }}</el-button>
          <el-button type="primary" @click="openAdd">{{ t('resources.addMachine') }}</el-button>
        </template>
      </div>
      <div v-else>
        <el-button text @click="openDocs">API</el-button>
        <el-button v-if="isAdmin" type="primary" @click="openAddCluster">{{ t('resources.addCluster') }}</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="resource-tabs">
      <el-tab-pane name="machines" :label="`${t('resources.machinesTab')} (${machines.length})`" />
      <el-tab-pane name="clusters" :label="`${t('resources.clusters')} (${clusters.length})`" />
    </el-tabs>

    <template v-if="tab === 'machines'">

    <div v-if="groups.length" class="groups">
      <h2 class="section-title">
        {{ t('resources.nodeGroups') }}
        <InfoHint :width="420"><span v-html="t('resources.nodeGroupsHint')" />
        </InfoHint>
      </h2>
      <div v-for="g in groups" :key="g.id" class="card group-card">
        <div class="top">
          <div class="who">
            <span class="name">{{ g.name }}</span>
            <el-tag size="small" effect="plain">{{ t('resources.nNodes', { n: g.node_count }) }}</el-tag>
            <el-tag :type="g.deployable ? 'success' : 'warning'" size="small"
              :title="g.deployable
                ? t('resources.group.readyTip')
                : t('resources.group.blockedTip')">
              {{ g.deployable ? t('resources.group.ready') : t('resources.group.blocked') }}
            </el-tag>
            <span v-if="g.driver" class="muted tiny mono">{{ g.driver }}</span>
            <span v-if="g.notes" class="muted tiny">· {{ g.notes }}</span>
          </div>
          <div class="acts">
            <el-button size="small" :loading="busy && preflightGroup === g.name"
              @click="runGroupPreflight(g)">{{ t('resources.preflight') }}</el-button>
            <el-button v-if="isAdmin" size="small" @click="openEditGroup(g)">{{ t('resources.edit') }}</el-button>
            <el-button v-if="isAdmin" size="small" text class="risky" @click="removeGroup(g)">
              {{ t('resources.dissolve') }}
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
              · {{ t('resources.cardsBusy', { busy: m.gpus_busy, n: m.gpu_count }) }}
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

    <PageLoading v-if="!machines.length && (first.loading || first.error)" :error="first.error"
      @retry="first.run" />
    <div v-else-if="!machines.length" class="empty muted">
      {{ t('resources.noMachines') }}
      <el-button v-if="isAdmin" link type="primary" @click="openAddCluster">{{ t('resources.addACluster') }}</el-button>
      <span v-else>{{ t('resources.adminAdds') }}</span>
    </div>

    <div v-for="m in machines" :key="m.id" class="card">
      <div class="top">
        <div class="who">
          <span class="name">{{ m.name }}</span>
          <el-tag v-if="m.launch_kind" size="small" effect="plain" :type="launchLook(m.launch_kind).type"
            :title="`${t('resources.launchedAs')}: ${t('resources.launchKindTip')}`">
            {{ launchLook(m.launch_kind).text }}
          </el-tag>
          <el-tag :type="(leaseLook(m).type as any)" size="small">{{ leaseLook(m).text }}</el-tag>
          <el-tag v-if="stageOf(m) && (m.lease_state === 'active' || m.lease_state === 'draining')"
            size="small" effect="plain"
            :type="(readinessLook[stageOf(m)!.readiness]?.type as any) ?? 'info'"
            :title="t('resources.readinessTip')">
            {{ readinessLook[stageOf(m)!.readiness]?.text ?? stageOf(m)!.readiness }}
          </el-tag>
        </div>
        <div class="acts">
          <el-button v-if="m.lease_state !== 'active' && m.lease_state !== 'draining'"
            size="small" type="primary" @click="lease(m)">
            {{ t('resources.leaseToPlatform') }}
          </el-button>
          <el-button v-else-if="m.lease_state === 'active'" size="small"
            @click="endLease(m, 'polite')">
            {{ t('resources.endLease') }}
          </el-button>
          <span v-else class="muted tiny">{{ t('resources.handingBack') }}</span>

          <el-button size="small" text @click="openSmoke(m)">{{ t('resources.smokeTest') }}</el-button>
          <el-dropdown trigger="click" @command="(a: any) => menu(m, a)">
            <el-button size="small" text :loading="busyMachine === m.id">{{ t('resources.more') }} ▾</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <template v-if="isAdmin">
                  <el-dropdown-item command="edit">{{ t('resources.edit') }}</el-dropdown-item>
                  <el-dropdown-item command="remove" class="risky">{{ t('resources.remove') }}</el-dropdown-item>
                </template>
                <el-dropdown-item command="probe" :divided="isAdmin">{{ t('resources.reread') }}</el-dropdown-item>
                <el-dropdown-item command="end-eager"
                  :disabled="m.lease_state === 'none'
                  || m.lease_state === 'released'" class="risky">
                  {{ t('resources.endNowStops') }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <div class="meta muted tiny">
        <span>{{ t('resources.cardsInUse', { busy: m.gpus_busy, n: m.gpu_count }) }}<template
          v-if="m.gpu_type"> · {{ m.gpu_type }}</template></span>
        <span v-if="m.driver === 'k8s' && m.gpu_free != null" :title="t('resources.freeTip')
          + (m.capacity_checked_at ? ` (${relativeTime(m.capacity_checked_at)})` : '')">
          {{ t('resources.freeOnCluster', { n: m.gpu_free }) }}</span>
        <span v-if="m.pool"
          :title="t('resources.poolTip', { n: m.node_count, selector: m.node_selector || t('resources.anyNode'), max: m.node_gpu_max })">
          {{ t('resources.pool', { n: m.node_count }) }}</span>
        <span v-if="m.driver === 'k8s'" :title="m.cluster_name
          ? t('resources.podsLand', { name: m.cluster_name }) : t('resources.defaultClusterTip')">
          {{ m.cluster_name || t('resources.defaultCluster') }}</span>
        <span v-if="m.host && m.host !== m.name" class="mono">{{ m.host }}</span>
        <span v-if="m.group" :title="m.group_rank === 0
          ? t('resources.masterOf', { group: m.group })
          : t('resources.rankOf', { group: m.group, rank: m.group_rank })">
          {{ m.group }} · {{ m.group_rank === 0 ? 'master' : `rank ${m.group_rank}` }}</span>
      </div>
      <div v-if="m.notes" class="notes muted tiny">{{ m.notes }}</div>

      <div v-if="m.lease_state === 'active' || m.lease_state === 'draining'" class="lease">
        <span class="muted tiny">
          {{ t('resources.leasedBy') }} <b>{{ m.lease_holder || t('resources.unknown') }}</b>
          <template v-if="m.lease_due_at"> · {{ t('resources.dueBack', { ago: relativeTime(m.lease_due_at) }) }}</template>
          <template v-if="stageOf(m)?.returnable_at">
            · {{ t('resources.freeBy', { time: exactClock(stageOf(m)!.returnable_at) }) }}
          </template>
        </span>
      </div>

      <div class="track">
        <div v-for="(label, i) in steps" :key="label" class="step" :class="stepClass(m, i)">
          <span class="pip" />
          <span class="label">{{ stepLabel(label) }}</span>
        </div>
      </div>

      <div class="say">
        <span class="dot" :class="dotClass[stageOf(m)?.state ?? 'waiting']" />
        <b>{{ stageOf(m)?.headline ?? '…' }}</b>
        <InfoHint v-if="stageOf(m)?.detail || stageOf(m)?.hand_back?.summary" :width="340">
          {{ stageOf(m)?.detail }}
          <template v-if="(m.lease_state === 'active' || m.lease_state === 'draining')
            && stageOf(m)?.hand_back?.summary">
            <br /><br /><b>{{ t('resources.onHandBack') }}</b> {{ stageOf(m)!.hand_back.summary }}
          </template>
        </InfoHint>
      </div>

      <div v-if="stageOf(m)?.campaigns?.length" class="for">
        <span class="muted tiny">{{ t('resources.for') }}</span>
        <LinkButton v-for="c in stageOf(m)!.campaigns" :key="c.id" link type="primary"
          size="small" :to="`/campaigns/${c.id}`">
          {{ c.name || t('resources.campaignN', { id: c.id }) }}
        </LinkButton>
      </div>

    </div>

    </template>

    <template v-else>
    <div v-for="c in clusters" :key="c.id" class="card">
      <div class="top">
        <div class="who">
          <span class="name">{{ c.name }}</span>
          <el-tag size="small" effect="plain" :type="launchLook(c.workload_kind).type"
            :title="t('resources.workloadKind')">
            {{ launchLook(c.workload_kind).text }}
          </el-tag>
          <el-tag v-if="!c.has_kubeconfig" size="small" type="danger">
            {{ t('resources.noCredential') }}
          </el-tag>
        </div>
        <div v-if="isAdmin" class="acts">
          <el-button size="small" type="primary" @click="openNodes(c)">{{ t('resources.nodes') }}</el-button>
          <el-button size="small" text :loading="probingCluster === c.id"
            @click="probeCluster(c)">{{ t('resources.probe') }}</el-button>
          <el-dropdown trigger="click" @command="(a: any) => a === 'edit' ? openEditCluster(c) : removeCluster(c)">
            <el-button size="small" text>{{ t('resources.more') }} ▾</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="edit">{{ t('resources.edit') }}</el-dropdown-item>
                <el-dropdown-item command="remove" class="risky">{{ t('resources.remove') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <div class="meta muted tiny">
        <ClusterHealth :probe="c.last_probe" :workload-kind="c.workload_kind"
          :title="c.last_probe_at ? t('resources.probedAgo', { ago: relativeTime(c.last_probe_at) }) : ''" />
        <span>{{ t('resources.nMachines', { n: c.machine_count ?? 0 }) }}</span>
        <span class="mono">{{ c.namespace }}</span>
        <span class="mono">{{ c.gpu_resource }}</span>
        <span>{{ c.api_mode }}</span>
        <span v-if="c.same_cluster === true">{{ t('resources.ownClusterShort') }}</span>
        <span v-else-if="c.same_cluster === false">{{ t('resources.anotherClusterShort') }}</span>
        <span v-if="c.node_host" class="mono">endpoint {{ c.node_host }}</span>
        <span v-if="c.runtime_class" class="mono">runtimeClass {{ c.runtime_class }}</span>
      </div>
      <div v-if="c.notes" class="notes muted tiny">{{ c.notes }}</div>
    </div>
    <div v-if="!clusters.length" class="empty muted">
      {{ t('resources.noClusters') }}
      <el-button v-if="isAdmin" link type="primary"
        @click="openAddCluster">{{ t('resources.addACluster') }}</el-button>
    </div>

    </template>

    <el-dialog v-model="showAdd" :title="editingId === null ? t('resources.addMachine') : t('resources.editMachine')"
      width="480px">
      <el-form label-position="top">
        <el-form-item :label="t('common.name')"><el-input v-model="form.name" /></el-form-item>
        <el-form-item>
          <template #label>
            {{ t('resources.driver') }}
            <!-- What has to be true before this machine can be reached at all,
                 set up outside this page. -->
            <InfoHint :width="380">
              <template v-if="form.driver === 'k8s'">
                <span v-html="t('resources.driverK8sHint')" />
              </template>
              <template v-else>
                <span v-html="t('resources.driverSshHint')" />
              </template>
              <a :href="setupDocs" target="_blank" rel="noopener">{{ t('resources.setupGuide') }}</a>
            </InfoHint>
          </template>
          <el-select v-model="form.driver" style="width: 100%" :empty-values="[null, undefined]">
            <el-option label="SSH + Docker" value="" />
            <el-option label="Kubernetes" value="k8s" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.driver === 'k8s'" :label="t('resources.cluster')">
          <el-select v-model="form.cluster_id" clearable :placeholder="t('resources.platformDefault')"
            style="width: 100%">
            <el-option v-for="c in clusters" :key="c.id"
              :label="`${c.name} · ${c.namespace} · ${c.workload_kind}`" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <template #label>
            {{ t('resources.launchedAs') }}
            <InfoHint><span>{{ t('resources.launchKindTip') }}</span></InfoHint>
          </template>
          <el-tag v-if="formLaunchKind" size="small" effect="plain" :type="launchLook(formLaunchKind).type">
            {{ launchLook(formLaunchKind).text }}
          </el-tag>
          <span v-else class="muted tiny">{{ t('resources.launchDefaultCluster') }}</span>
        </el-form-item>
        <el-form-item v-if="form.driver !== 'k8s'" :label="t('resources.host')">
          <el-input v-model="form.host" class="mono" placeholder="10.0.0.24" />
        </el-form-item>
        <el-form-item v-if="form.driver === 'k8s'">
          <template #label>
            {{ t('resources.nodeSelector') }}
            <InfoHint><span v-html="t('resources.nodeSelectorHint')" /></InfoHint>
          </template>
          <el-input v-model="form.node_selector" class="mono" placeholder="label=value" />
        </el-form-item>
        <el-checkbox v-if="editingId === null" v-model="leaseOnAdd" class="lease-now">
          {{ t('resources.leaseToPlatform') }}
        </el-checkbox>
        <el-collapse v-model="addAdvanced" class="dialog-advanced">
          <el-collapse-item name="advanced" :title="t('resources.advanced')">
        <div v-if="form.driver !== 'k8s'" class="grid-2">
          <el-form-item :label="t('resources.sshUser')"><el-input v-model="form.ssh_user" /></el-form-item>
          <el-form-item :label="t('resources.sshPort')">
            <el-input-number v-model="form.ssh_port" :min="1" :max="65535" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item>
            <template #label>
              {{ t('resources.gpuCount') }}
              <InfoHint>{{ t('resources.gpuCountHint') }}</InfoHint>
            </template>
            <el-input-number v-model="form.gpu_count" :min="1" :max="64"
              :value-on-clear="null" :placeholder="t('resources.readOnSave')" controls-position="right" />
          </el-form-item>
          <el-form-item :label="t('resources.gpuType')">
            <el-select v-model="form.gpu_type" clearable :placeholder="t('resources.anyCard')"
              style="width: 100%">
              <el-option v-for="g in gpuTypes" :key="g" :label="g" :value="g" />
            </el-select>
          </el-form-item>
        </div>
        <template v-if="form.driver !== 'k8s'">
          <el-form-item>
            <template #label>
              {{ t('resources.interior') }}
              <InfoHint :width="360">{{ t('resources.interiorHint') }}</InfoHint>
            </template>
            <el-input v-model="form.data_host" class="mono" :placeholder="t('resources.sameAsHost')" />
          </el-form-item>
          <el-form-item>
            <template #label>
              {{ t('resources.ncclIf') }}
              <InfoHint><span v-html="t('resources.ncclIfHint')" /></InfoHint>
            </template>
            <el-input v-model="form.nccl_ifname" class="mono" :placeholder="t('resources.engineDefault')" />
          </el-form-item>
        </template>
        <el-form-item :label="t('resources.notes')"><el-input v-model="form.notes" /></el-form-item>
          </el-collapse-item>
        </el-collapse>
      </el-form>
      <template #footer>
        <el-button @click="showAdd = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="busy" @click="save">
          {{ editingId === null ? t('resources.add') : t('common.save') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showGroup" width="560px"
      :title="editingGroupId === null ? t('resources.groupTitle') : t('resources.editGroup')">
      <el-form label-position="top">
        <el-form-item :label="t('common.name')">
          <el-input v-model="groupForm.name" :disabled="editingGroupId !== null"
            :placeholder="t('resources.egGroup')" />
        </el-form-item>
        <el-form-item>
          <template #label>
            {{ t('resources.members') }}
            <InfoHint :width="340">{{ t('resources.membersHint') }}</InfoHint>
          </template>
          <el-select v-model="groupForm.members" multiple style="width: 100%">
            <el-option v-for="c in memberChoices" :key="c.name" :label="c.label"
              :value="c.name" :disabled="c.disabled" />
          </el-select>
        </el-form-item>
        <div class="grid-2">
          <el-form-item :label="t('resources.substrate')">
            <el-select v-model="groupForm.driver" style="width: 100%" :empty-values="[null, undefined]">
              <el-option :label="t('resources.eachOwn')" value="" />
              <el-option label="SSH + Docker" value="ssh_docker" />
              <el-option label="Kubernetes" value="k8s" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <template #label>
              {{ t('resources.rendezvous') }}
              <InfoHint>{{ t('resources.rendezvousHint') }}</InfoHint>
            </template>
            <el-input-number v-model="groupForm.dist_port" :min="0" :max="65535" />
          </el-form-item>
        </div>
        <el-form-item>
          <template #label>
            {{ t('resources.ncclEnv') }}
            <InfoHint><span v-html="t('resources.ncclEnvHint')" /></InfoHint>
          </template>
          <el-input v-model="groupForm.ncclEnvText" type="textarea" :rows="3" class="mono"
            placeholder="NCCL_SOCKET_IFNAME=ib0" />
        </el-form-item>
        <el-form-item :label="t('resources.notes')"><el-input v-model="groupForm.notes" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showGroup = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="busy"
          :disabled="!groupForm.name.trim() || !groupForm.members.length" @click="saveGroup">
          {{ editingGroupId === null ? t('resources.createGroup') : t('common.save') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showGroupPreflight" :title="`${t('resources.preflight')} · ${preflightGroup}`" width="640px">
      <div v-if="!groupPreflight" class="muted">{{ t('resources.probing') }}</div>
      <template v-else>
        <el-alert v-if="groupPreflight.note" type="info" :closable="false"
          :title="groupPreflight.note" style="margin-bottom: 12px" />
        <el-alert v-if="groupPreflight.ok" type="success" :closable="false" show-icon
          :title="t('resources.pfOk')"
          style="margin-bottom: 12px" />
        <el-alert v-else-if="!groupPreflight.note" type="error" :closable="false" show-icon
          :title="t('resources.pfBad')" style="margin-bottom: 12px" />
        <div v-for="row in groupPreflight.machines" :key="row.machine" class="pf-row">
          <div class="pf-head">
            <span class="mono">{{ row.machine }}</span>
            <el-tag size="small" :type="row.ok ? 'success' : 'danger'" effect="plain">
              {{ row.ok ? 'ok' : t('resources.nFailed', { n: row.failed }) }}
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
        <el-button @click="showGroupPreflight = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>

    <SmokeTestDialog v-model="showSmoke" :machine="smokeMachine" />

    <el-dialog v-model="showAddCluster" width="680px"
      :title="addStep === 'connect' ? t('resources.addCluster') : `${t('resources.gpuNodes')} · ${nodesCluster?.name ?? ''}`">
      <el-steps :active="addStep === 'connect' ? 0 : 1" simple class="wizard-steps">
        <el-step :title="t('resources.wizard.connect')" />
        <el-step :title="t('resources.wizard.register')" />
      </el-steps>

      <el-form v-if="addStep === 'connect'" label-position="top">
        <el-form-item>
          <template #label>
            Kubeconfig
            <InfoHint :width="380">
              <span v-html="t('resources.kubeconfigHint')" />
              <a :href="setupDocs" target="_blank" rel="noopener">{{ t('resources.setupGuide') }}</a>
            </InfoHint>
            <el-button link type="primary" class="label-link"
              @click="kubeconfigFile?.click()">{{ t('resources.upload') }}</el-button>
          </template>
          <input ref="kubeconfigFile" type="file" hidden @change="readKubeconfigFile" />
          <el-input v-model="newCluster.kubeconfig" type="textarea" :rows="6" class="mono"
            :placeholder="t('resources.pasteKubeconfig')" />
        </el-form-item>
        <div class="grid-2">
          <el-form-item :label="t('common.name')">
            <el-input v-model="newCluster.name" placeholder="gpu-cluster-a" />
          </el-form-item>
          <el-form-item :label="t('resources.namespace')"
            :error="namespaceMissing && !newCluster.namespace.trim() ? t('resources.wizard.namespaceNeeded') : ''">
            <el-input v-model="newCluster.namespace" class="mono"
              :placeholder="kubeFacts.namespace || t('resources.fromKubeconfig')" />
          </el-form-item>
        </div>
        <ClusterEndpointFields v-model:host="newCluster.node_host"
          v-model:port="newCluster.service_nodeport" v-model:endpoint="newCluster.endpoint"
          :workload-kind="newCluster.workload_kind" />
        <el-collapse v-model="clusterAdvanced" class="dialog-advanced">
          <el-collapse-item name="advanced" :title="t('resources.advanced')">
            <div class="grid-2">
              <el-form-item :label="t('resources.workloadKind')">
                <el-select v-model="newCluster.workload_kind" style="width: 100%">
                  <el-option :label="t('resources.wkLlmservice')" value="llmservice" />
                  <el-option :label="`${t('resources.wkDeployment')} ${t('resources.wkDeprecated')}`" value="deployment" />
                  <el-option :label="`custom (TuningRun CRD) ${t('resources.wkDeprecated')}`" value="custom" />
                </el-select>
              </el-form-item>
              <el-form-item label="RuntimeClass">
                <el-input v-model="newCluster.runtime_class" class="mono" />
              </el-form-item>
              <el-form-item :label="t('resources.gpuResource')">
                <el-input v-model="newCluster.gpu_resource" class="mono" />
              </el-form-item>
              <el-form-item :label="t('resources.gpuLabel')">
                <el-input v-model="newCluster.gpu_product_label" class="mono"
                  placeholder="node.kubernetes.io/npu.chip.name" />
              </el-form-item>
              <el-form-item :label="t('resources.defaultSelector')">
                <el-input v-model="newCluster.node_selector" class="mono" :placeholder="t('resources.none')" />
              </el-form-item>
              <el-form-item>
                <template #label>
                  {{ t('resources.policySelector') }}
                  <InfoHint :width="360"><span v-html="t('resources.policySelectorHint')" /></InfoHint>
                </template>
                <el-input v-model="newCluster.policy_node_selector" class="mono"
                  :placeholder="t('resources.installSetting')" />
              </el-form-item>
              <el-form-item>
                <template #label>
                  {{ t('resources.tolerations') }}
                  <InfoHint :width="360"><span v-html="t('resources.tolerationsHint')" /></InfoHint>
                </template>
                <el-input v-model="newCluster.tolerations" class="mono"
                  placeholder="node.kubernetes.io/unschedulable" />
              </el-form-item>
              <el-form-item :label="t('resources.pullSecrets')">
                <el-input v-model="newCluster.image_pull_secrets" class="mono" :placeholder="t('resources.none')" />
              </el-form-item>
              <template v-if="newCluster.workload_kind === 'llmservice'">
                <el-form-item :label="t('resources.chartRepo')">
                  <el-input v-model.trim="newCluster.chart_repo" class="mono"
                    placeholder="https://modelsphere.github.io/helm-charts" />
                </el-form-item>
                <el-form-item :label="t('resources.imageMirror')">
                  <el-input v-model.trim="newCluster.image_mirror" class="mono" :placeholder="t('resources.none')" />
                </el-form-item>
              </template>
            </div>
          </el-collapse-item>
        </el-collapse>
        <div v-if="connectFailure" class="connect-failure">
          <div class="failure-head">
            <el-icon :size="16"><Warning /></el-icon>
            <b>{{ t('resources.wizard.unreachable') }}</b>
          </div>
          <ClusterHealth :probe="connectFailure.probe" :workload-kind="newCluster.workload_kind" />
          <ul class="muted tiny">
            <li v-for="w in connectFailure.probe.warnings ?? []" :key="w">{{ w }}</li>
          </ul>
        </div>
      </el-form>

      <template v-else>
        <div v-if="nodesError" class="pf-check">
          <el-alert type="error" :closable="false" show-icon :title="nodesError" />
        </div>
        <div v-else-if="!nodesView" class="muted">{{ t('resources.readingNodes') }}</div>
        <template v-else>
          <div class="nodes-head">
            <ClusterHealth :probe="nodesClusterRow?.last_probe" :workload-kind="nodesClusterRow?.workload_kind ?? 'llmservice'" />
            <span class="muted tiny">
              <span class="mono">{{ nodesView.namespace }}</span>
              <template v-if="nodesView.nodes_readable"> · {{ t('resources.nGpuNodes', { n: nodesView.nodes.length }) }}</template>
            </span>
          </div>

          <!-- A namespace-scoped credential: no nodes, the quota is the pool. -->
          <div v-if="!nodesView.nodes_readable" class="quota-panel">
            <el-icon :size="28" class="quota-icon"><Lock /></el-icon>
            <div class="quota-body">
              <b>{{ t('resources.wizard.noNodeAccess') }}</b>
              <div v-if="nodesView.namespace_machine" class="muted tiny">
                {{ t('resources.registeredAs', { name: nodesView.namespace_machine.name }) }}
              </div>
              <div v-else-if="nodesView.quota" class="quota-line">
                <span class="quota-n">{{ nodesView.quota.gpus }}</span>
                <span class="muted">{{ t('resources.wizard.quota', { n: nodesView.quota.gpus }) }}</span>
              </div>
              <div v-else class="muted tiny">{{ t('resources.wizard.noQuota') }}</div>
            </div>
          </div>

          <template v-else>
          <div class="register-as">
            <el-radio-group v-model="registerAs" size="small">
              <el-radio-button value="pools">{{ t('resources.asPools') }}</el-radio-button>
              <el-radio-button value="nodes">{{ t('resources.asNodes') }}</el-radio-button>
            </el-radio-group>
            <InfoHint v-if="registerAs === 'pools'">{{ t('resources.poolsHint') }}</InfoHint>
            <InfoHint v-else>{{ t('resources.nodesHint') }}</InfoHint>
            <span class="spacer" />
            <el-tooltip v-if="cordonedNodes.length && !tolerated" :content="t('resources.wizard.useCordonedTip')"
              placement="top">
              <el-button size="small" :loading="busy" @click="useCordoned">
                <el-icon><Lock /></el-icon>&nbsp;{{ t('resources.wizard.useCordoned') }} ({{ cordonedNodes.length }})
              </el-button>
            </el-tooltip>
          </div>
          <el-table v-if="registerAs === 'pools'" :data="nodesView.pools" size="small"
            row-key="product" :empty-text="t('resources.noGpuNodes')">
            <el-table-column width="48">
              <template #default="{ row }">
                <el-checkbox v-if="!row.machine" :disabled="row.single_nodes.length > 0"
                  :model-value="pickedPools.includes(row.product)"
                  @update:model-value="(on: any) => pickedPools = on
                    ? [...pickedPools, row.product]
                    : pickedPools.filter((p) => p !== row.product)" />
              </template>
            </el-table-column>
            <el-table-column label="GPU" min-width="150">
              <template #default="{ row }">
                {{ row.gpu_type || row.product }}
                <div class="muted tiny mono">{{ row.product }}</div>
              </template>
            </el-table-column>
            <el-table-column :label="t('resources.nodes')" min-width="190">
              <template #default="{ row }">
                <div class="usable" :title="row.nodes.join(', ')">
                  <span v-for="i in row.nodes.length" :key="i" class="pip"
                    :class="{ on: i <= row.nodes_usable }" />
                  <span class="tiny">{{ t('resources.nCards', { n: row.gpu_count }) }}</span>
                </div>
                <div class="muted tiny">{{ t('resources.wizard.usable', { n: row.nodes_usable, total: row.nodes.length }) }}</div>
              </template>
            </el-table-column>
            <el-table-column :label="t('common.status')" min-width="150">
              <template #default="{ row }">
                <el-tag v-if="row.machine" size="small" type="success" effect="plain">
                  {{ t('resources.registeredAs', { name: row.machine.name }) }}
                </el-tag>
                <el-tag v-else-if="row.single_nodes.length" size="small" type="warning" effect="plain"
                  :title="row.single_nodes.join(', ')">
                  {{ t('resources.wizard.alone', { n: row.single_nodes.length }) }}
                </el-tag>
                <span v-else class="muted tiny">{{ t('resources.new') }}</span>
              </template>
            </el-table-column>
          </el-table>
          <el-table v-else :data="nodesView.nodes" size="small" row-key="hostname"
            :empty-text="t('resources.noGpuNodes')">
            <el-table-column width="48">
              <template #default="{ row }">
                <el-checkbox v-if="!row.machine" :disabled="!!row.pool"
                  :model-value="pickedNodes.includes(row.hostname)"
                  @update:model-value="(on: any) => pickedNodes = on
                    ? [...pickedNodes, row.hostname]
                    : pickedNodes.filter((h) => h !== row.hostname)" />
              </template>
            </el-table-column>
            <el-table-column :label="t('resources.node')" min-width="160">
              <template #default="{ row }">
                <span class="mono">{{ row.node }}</span>
                <el-icon v-if="row.ready && !row.schedulable" class="cordon-icon"
                  :title="t('resources.cordoned')"><Lock /></el-icon>
              </template>
            </el-table-column>
            <el-table-column label="GPUs" min-width="130">
              <template #default="{ row }">
                {{ row.gpu_count }}× {{ row.gpu_type || row.gpu_product || 'GPU' }}
              </template>
            </el-table-column>
            <el-table-column :label="t('common.status')" min-width="170">
              <template #default="{ row }">
                <el-tag v-if="row.machine" size="small" type="success" effect="plain">
                  {{ t('resources.registeredAs', { name: row.machine.name }) }}
                </el-tag>
                <el-tag v-else-if="row.pool" size="small" effect="plain">
                  {{ t('resources.wizard.inPool', { name: row.pool.name }) }}
                </el-tag>
                <el-tag v-else-if="!row.ready" size="small" type="danger" effect="plain">
                  {{ t('resources.notReady') }}
                </el-tag>
                <el-tag v-else-if="!row.takes_pods" size="small" type="warning" effect="plain">
                  {{ t('resources.cordoned') }}
                </el-tag>
                <span v-else class="muted tiny">{{ t('resources.new') }}</span>
              </template>
            </el-table-column>
          </el-table>
          </template>
          <el-checkbox v-if="nodesView.nodes_readable
              ? (registerAs === 'pools' ? nodesView.pools.some((p) => !p.machine) : nodesView.nodes.some((n) => !n.machine))
              : !!nodesView.quota && !nodesView.namespace_machine"
            v-model="leaseOnRegister"
            class="lease-now">
            {{ t('resources.leaseNow') }}
            <InfoHint>{{ t('resources.leaseNowHint') }}</InfoHint>
          </el-checkbox>
          <el-alert v-if="nodesView.gone.length" type="warning" :closable="false" show-icon
            class="gone"
            :title="t('resources.gone', { names: nodesView.gone.map((g) => g.name).join(', ') })" />
        </template>
      </template>

      <template #footer>
        <el-button @click="showAddCluster = false">
          {{ addStep === 'connect' ? t('common.cancel') : t('common.close') }}
        </el-button>
        <el-button v-if="addStep === 'connect'" type="primary" :loading="busy"
          :disabled="!newCluster.kubeconfig.trim() || !newCluster.name.trim()
            || (namespaceMissing && !newCluster.namespace.trim())"
          @click="connectCluster">{{ t('resources.connect') }}</el-button>
        <el-button v-else-if="nodesView && !nodesView.nodes_readable" type="primary" :loading="busy"
          :disabled="!nodesView.quota || !!nodesView.namespace_machine"
          @click="registerNamespacePool">{{ t('resources.wizard.asOnePool') }}</el-button>
        <el-button v-else type="primary" :loading="busy" :disabled="!picked"
          @click="registerNodes">
          {{ registerAs === 'pools' ? t('resources.registerPools', { n: picked || '' }) : t('resources.registerNodes', { n: picked || '' }) }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showClusters" :title="t('resources.editName', { name: clusterForm.name })" width="680px">
      <template v-if="editingClusterId !== null">
      <el-form label-position="top">
        <div class="grid-2">
          <el-form-item :label="t('common.name')">
            <el-input v-model="clusterForm.name" placeholder="gpu-cluster-a" />
          </el-form-item>
          <el-form-item :label="t('resources.namespace')">
            <el-input v-model="clusterForm.namespace" class="mono" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item :label="t('resources.apiMode')">
            <el-select v-model="clusterForm.api_mode" style="width: 100%">
              <el-option label="client (Python client)" value="client" />
              <el-option label="kubectl (shell out)" value="kubectl" />
              <el-option label="unavailable" value="unavailable" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('resources.workloadKind')">
            <el-select v-model="clusterForm.workload_kind" style="width: 100%">
              <el-option :label="t('resources.wkLlmservice')" value="llmservice" />
              <el-option :label="`${t('resources.wkDeployment')} ${t('resources.wkDeprecated')}`" value="deployment" />
              <el-option :label="`custom (TuningRun CRD) ${t('resources.wkDeprecated')}`" value="custom" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item>
          <template #label>
            Kubeconfig
            <InfoHint>{{ t('resources.kubeconfigEditHint') }}</InfoHint>
          </template>
          <el-input v-model="clusterForm.kubeconfig" type="textarea" :rows="5" class="mono"
            :placeholder="editingClusterId === null
              ? t('resources.pasteYaml')
              : t('resources.keepCred')" />
        </el-form-item>
        <ClusterEndpointFields v-model:host="clusterForm.node_host"
          v-model:port="clusterForm.service_nodeport" v-model:endpoint="clusterForm.endpoint"
          :workload-kind="clusterForm.workload_kind"
          :same-cluster="clusters.find((c) => c.id === editingClusterId)?.same_cluster ?? null" />
        <template v-if="clusterForm.workload_kind === 'llmservice'">
          <div class="grid-2">
            <el-form-item :label="t('resources.chartRepo')">
              <el-input v-model.trim="clusterForm.chart_repo" class="mono"
                placeholder="https://modelsphere.github.io/helm-charts" />
              <div class="muted small">{{ t('resources.chartRepoHint') }}</div>
            </el-form-item>
            <el-form-item :label="t('resources.chartCredentials')">
              <el-input v-model.trim="clusterForm.chart_credentials_secret" class="mono"
                :placeholder="t('resources.none')" />
            </el-form-item>
          </div>
          <el-form-item :label="t('resources.imageMirror')">
            <el-input v-model.trim="clusterForm.image_mirror" class="mono"
              :placeholder="t('resources.none')" />
            <div class="muted small">{{ t('resources.imageMirrorHint') }}</div>
          </el-form-item>
          <el-form-item :label="t('resources.siteValues')">
            <el-input v-model="clusterForm.site_values" type="textarea" :rows="3" class="mono"
              placeholder='{"securityContext": {"capabilities": {"add": ["IPC_LOCK"]}}}' />
            <div class="muted small">{{ t('resources.siteValuesHint') }}</div>
          </el-form-item>
        </template>
        <div class="grid-2">
          <el-form-item label="RuntimeClass">
            <el-input v-model="clusterForm.runtime_class" class="mono" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item :label="t('resources.gpuResource')">
            <el-input v-model="clusterForm.gpu_resource" class="mono" />
          </el-form-item>
          <el-form-item :label="t('resources.gpuLabel')">
            <el-input v-model="clusterForm.gpu_product_label" class="mono"
              placeholder="node.kubernetes.io/npu.chip.name" />
          </el-form-item>
          <el-form-item label="/dev/shm (MiB)">
            <el-input-number v-model="clusterForm.shm_size_mb" :min="0" :max="1048576" />
          </el-form-item>
        </div>
        <el-form-item :label="t('resources.defaultSelector')">
          <el-input v-model="clusterForm.node_selector" class="mono" :placeholder="t('resources.none')" />
        </el-form-item>
        <el-form-item>
          <template #label>
            {{ t('resources.policySelector') }}
            <InfoHint :width="360"><span v-html="t('resources.policySelectorHint')" /></InfoHint>
          </template>
          <el-input v-model="clusterForm.policy_node_selector" class="mono"
            :placeholder="t('resources.installSetting')" />
        </el-form-item>
        <el-form-item>
          <template #label>
            {{ t('resources.tolerations') }}
            <InfoHint :width="360"><span v-html="t('resources.tolerationsHint')" /></InfoHint>
          </template>
          <el-input v-model="clusterForm.tolerations" class="mono"
            placeholder="node.kubernetes.io/unschedulable" />
        </el-form-item>
        <el-form-item :label="t('resources.pullSecrets')">
          <el-input v-model="clusterForm.image_pull_secrets" class="mono"
            :placeholder="t('resources.noneCreds')" />
        </el-form-item>
        <div class="grid-2">
          <el-form-item :label="t('resources.cpuReq')">
            <el-input v-model="clusterForm.engine_cpu_request" class="mono" :placeholder="t('resources.omit')" />
          </el-form-item>
          <el-form-item :label="t('resources.memReq')">
            <el-input v-model="clusterForm.engine_memory_request" class="mono" :placeholder="t('resources.omit')" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item :label="t('resources.cpuLim')">
            <el-input v-model="clusterForm.engine_cpu_limit" class="mono" :placeholder="t('resources.omit')" />
          </el-form-item>
          <el-form-item :label="t('resources.memLim')">
            <el-input v-model="clusterForm.engine_memory_limit" class="mono" :placeholder="t('resources.omit')" />
          </el-form-item>
        </div>
        <el-form-item :label="t('resources.notes')">
          <el-input v-model="clusterForm.notes" />
        </el-form-item>
      </el-form>
      </template>
      <template #footer>
        <el-button @click="showClusters = false">{{ t('common.close') }}</el-button>
        <el-button v-if="editingClusterId !== null" type="primary" :loading="busy"
          @click="saveCluster">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <LiveWorkDialog v-model="liveWork.open" :message="liveWork.message"
      :runs="liveWork.runs" :sessions="liveWork.sessions" />
  </div>
</template>

<style scoped>
.dialog-advanced {
  border-top: none;
}
.register-as {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.nodes-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.wizard-steps {
  margin-bottom: 14px;
}
.register-as .spacer {
  flex: 1;
}
/* A pool's nodes as pips: lit ones take pods. */
.usable {
  display: flex;
  align-items: center;
  gap: 3px;
}
.usable .pip {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--el-border-color);
}
.usable .pip.on {
  background: var(--el-color-success);
}
.usable .tiny {
  margin-left: 6px;
}
.cordon-icon {
  margin-left: 6px;
  color: var(--el-color-warning);
  vertical-align: -2px;
}
.quota-panel {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
  border: 1px dashed var(--el-border-color);
  border-radius: 8px;
}
.quota-icon {
  color: var(--el-text-color-placeholder);
}
.quota-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 4px;
}
.quota-n {
  font-size: 22px;
  font-weight: 600;
}
.connect-failure {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--el-color-danger-light-9);
}
.failure-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  color: var(--el-color-danger);
}
.connect-failure ul {
  margin: 6px 0 0;
  padding-left: 18px;
}
.lease-now {
  margin-top: 10px;
}
.gone {
  margin-top: 10px;
}
.label-link {
  margin-left: 10px;
  font-size: 12px;
  font-weight: 400;
}
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
}
.who {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  min-width: 0;
}
.name {
  font-weight: 600;
  font-size: 15px;
}
.acts {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.tiny {
  font-size: 11.5px;
}
/* Everything past the name and state, on one quiet line under the top row;
   notes get a line of their own so they never push the buttons down. */
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 6px;
}
.notes {
  margin-top: 2px;
}
.resource-tabs {
  margin-bottom: 4px;
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
.for {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}
.empty {
  padding: 24px;
  text-align: center;
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
