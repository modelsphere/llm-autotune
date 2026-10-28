[User guide](README.md) › **Machines and GPU clusters**

# Machines and GPU clusters

The **Resources** page manages where runs happen. A **machine** is one place a
run can land:

- **The GPUs of a Kubernetes cluster.** Add the cluster once, then register
  each GPU type as a **pool** (the default: runs go to whichever node of that
  type has room) or each node as its own machine. This is the usual setup.
- **A bare-metal machine reached over ssh.** The platform connects and starts
  the engine with `docker run`.

The platform uses a machine only while it is **leased**.

## Adding a GPU cluster

*Admins only;* other users do not see **Add GPU cluster**. First run
`deploy/gpu-cluster.sh --context <gpu-cluster>` with the cluster's admin
kubeconfig ([Installing, step 2](../install.md#2-prepare-a-gpu-cluster)). It
creates a namespace for runs (`llm-autotune-runs`) and a ServiceAccount that
can manage only the platform's own workloads there and read nodes, and writes
`llm-autotune-runs.kubeconfig` holding only that account's token.

Press **Add GPU cluster** and upload the file. The name and namespace are
filled in from it; the namespace field asks for one only when the kubeconfig
names none. Press **Connect**: the platform tries the cluster first and keeps
it only if it can reach it. If it cannot, nothing is saved and the dialog shows
which check failed.

![Add GPU cluster](images/add-cluster-connect.png)

The kubeconfig is stored encrypted and never shown again. **Advanced** holds
settings most clusters do not need:

| setting | when to change it |
|---|---|
| **Workload kind** | `llmservice` (default) creates each run as an LLMService; `deployment` and `custom` are deprecated. |
| **RuntimeClass** | The runtime class engine pods request, `nvidia` by default. |
| **GPU resource** | The resource name GPUs are requested by, `nvidia.com/gpu` by default. |
| **Extra tolerations** | For GPU nodes with taints other than the standard GPU taint, which is tolerated automatically. Comma-separated `key[=value][:effect]`. |
| **Image pull secrets** | For engine images in a registry that requires credentials: names of secrets in the runs' namespace. |
| **Default node selector**, **Policy node selector** | Where engine pods, and the pods of policies the platform runs for you, may go. |
| **Chart repository**, **Image mirror** | LLMService only: where the operator pulls charts and images from. |

After **Connect**, a row of checks shows what the platform can do there: reach
the API, read nodes, find llm-operator. Then it lists the cluster's GPU nodes
and their GPUs, and offers two ways to register them:

- **One pool per GPU type** (the default): one machine per GPU type, spanning
  every node of that type ([Node pools](#node-pools)).
- **One machine per node:** each machine is pinned to its node, and its runs
  wait for that node's cards even when another node is free.

Select what to register and press **Register**. What can take work now is
selected by default. A pool's nodes show as small squares, lit for each node
that takes pods; only those nodes' cards count, and never more than the
namespace's GPU quota. A node can be in a pool or registered on its own, not
both, so its cards are never counted twice. With **Lease them to the platform
now** selected (the default), the new machines are leased immediately.

Nodes an admin cordoned for the platform show a lock. **Use cordoned nodes**
lets the cluster's pods land on them (it adds the cordon's toleration).

When the credential cannot read nodes, the namespace's GPU quota is offered as
one pool instead: **Register as one pool**.

![Registering a cluster's GPU nodes](images/add-cluster-nodes.png)

### The Clusters tab

The **Clusters** tab, next to **Machines**, lists the added clusters. Each card
shows the cluster's name and how it launches runs (**LLMService**, or a
deprecated kind). The line below starts with the last probe's checks (hover a
warning for why), then its machines, namespace and GPU resource.

- **Nodes** registers nodes added to the cluster since, and lists registered
  machines whose node no longer exists.
- **Probe** checks that the credential still works and refreshes the cluster's
  details.
- **More ▸ Edit** changes its settings, including some available only here.
  The workload kind and namespace cannot change while the cluster has live
  runs: the platform finds its pods by them. The refusal lists those runs, and
  marks any still tearing down, which may be stuck.
  - **Endpoint host**, the address runs are reached on (by default the node's
    own address);
  - the size of `/dev/shm` in engine pods;
  - a **Default node selector**;
  - CPU and memory requests and limits for engine pods.

  Leave **Kubeconfig** empty to keep the stored one, or paste a new one to
  replace it.
- **More ▸ Remove** deletes the cluster and its stored kubeconfig from the platform.
  Its machines must be removed (or moved to another cluster) first. Nothing on
  the cluster changes; `deploy/gpu-cluster.sh remove` revokes the account
  there.

## The machine card

![A leased machine](images/machine-leased.png)

- **Name.** For a cluster node, the node's name.
- **Tags:** how its runs launch (**LLMService**; **Deployment**, **TuningRun**
  and **SSH + Docker** are deprecated), and the lease state: *not leased*, *leased*, *running* (runs in progress) or *handing
  back*. While leased, also the readiness reported to the lease API: *idle*,
  *busy* or *returnable*.
- **The line below** holds the rest, in small type. **Cards in use**, out of
  the machine's total, and the GPU type when known.
  On a cluster, also the cards **free on the cluster**: those no pod holds, on
  nodes that take new pods. It needs the platform to read pods in every
  namespace, which is off by default (`gpuCluster.countFreeCards` in the chart,
  `gpu-cluster.sh --count-free-cards`); without it the count is not shown.
  Then **pool · N nodes** for a node pool, the cluster's name, the host and
  the node group, when they apply.
- **Notes**, on a line of their own.

The track below shows the lease stage: **Leased**, **Running campaigns**,
**Handed back**. The sentence under it describes the current state; its **?**
explains what ending the lease would do. Campaigns using the machine are
linked below.

## Leases

**Lease to platform** lets campaigns use the machine. Runs start on it only
while it is leased, and only inside a campaign's window.

![A registered machine that is not leased](images/machine-registered.png)

To end the lease:

- **End lease:** runs in progress finish and nothing new starts (the machine
  shows *handing back*), then the machine is released. The dialog shows the
  expected wait.
- **More ▸ End lease now (stops runs):** stops the platform's runs on the
  machine immediately; their measurements are lost.

In both cases only the platform's own runs stop; nothing else on the machine
is touched.

Another system, such as a scheduler that leases idle GPUs at night, can lease
machines through the [machine lease API](../api/machine-lease.md). **API** on
this page opens the API reference.

## Smoke test

**Smoke test** checks that the platform can reach the machine and has the
permissions a run needs. On a cluster node it checks the credential, the
permissions, that the node is ready with free GPUs, and that runs on it are
reachable. Select **Probe pod** and press **Run again** to also schedule a
small pod on the node, requesting no GPUs.

![Smoke test results](images/smoke-test.png)

A *warn* line does not block runs. A *fail* line names what to fix.

## More ▸

Adding, editing and removing machines is for admins.

- **Edit** changes the machine's name, GPU count, GPU type and notes.
- **Remove** deletes it from the platform; its finished runs are kept. On a
  cluster, **Clusters ▸ Nodes** can register the node again.
- **Re-read GPUs** reads the GPU count and type from the machine again.

Set the **GPU type** (such as `H100`) when the platform cannot detect it.
Results are compared with a baseline only on the same GPU type.

A machine cannot be edited or removed while an unfinished campaign uses it.
Close those campaigns first: **⋯ ▸ Mark as done** on a stopped campaign, or
**⋯ ▸ Delete** on one that never ran.

## Bare-metal machines over ssh

The worker connects over ssh and runs the engine with `docker run`.

1. Give the worker an ssh key at install time (`worker.ssh` in the values; see
   [After installing](../after-installing.md#bare-metal-machines-over-ssh)).
2. Add the key's public half to the ssh user's `~/.ssh/authorized_keys` on the
   machine. The machine needs Docker and the NVIDIA container toolkit.
3. Press **Add machine**, keep **Driver** at *SSH + Docker*, and enter a
   **Name** and the **Host** address. The GPU count and type are read over ssh
   when you save; if the machine cannot be read, the save is refused and
   **Advanced** opens so you can enter the GPU count. **Advanced** also has the
   ssh user and port, and notes.
4. **Smoke test** the machine, then **Lease to platform**.

**Add machine** with the *Kubernetes* driver is for a special case: a slice of
an added cluster, limited by a **Node selector** (`label=value`), for example
for a model whose weights are only on some nodes. **Add GPU cluster** already
registers pools or nodes, so this is rarely needed.

## Node pools

A pool is a machine whose node selector names a GPU type
(`nvidia.com/gpu.product=…`) instead of one node. The cluster decides where
each run goes:

- **Each run lands on whichever node of the pool has room.** All of one run's
  cards are on one node. The run's page shows the node it landed on.
- **When no node has room, the run waits.** It shows **waiting for cards**,
  with the cluster's reason in its log, and starts on its own when cards free
  up. Waiting never fails the run; the campaign's window or the machine's lease
  ends it. Its startup time counts from when it got its cards.
- **A run no node could ever hold fails at once**, for example one needing 16
  cards on 8-card nodes.
- **Campaigns share a pool** whether or not they allow sharing a machine.
- **A policy campaign** takes as many cards at a time as its widest
  configuration needs, or `policy_settings.cards` from the campaign YAML. A
  finalist the cluster never found cards for is marked *skipped*, not
  *failed*.
- **A policy that runs engines in its own container** (*Give the container the
  GPUs*) needs all its cards on one node and does not use pools; register single
  nodes for it.
- **A pool holds one GPU type.** Leasing a selector that matches two types is
  refused, because their results are not comparable.

## Node groups (multi-node)

A model too large for one node can be served across several. **Group
machines** joins leased machines into a named group; the first one selected is
the master. Members must share a driver, a GPU type and a GPU count.

A group is not a reservation: each member remains an ordinary machine other
campaigns can use, and is occupied only while a multi-node run is deployed on
it. **Preflight** on a group checks that every member is ready and can reach
the master. A campaign selects the group on its **Machines** step (sglang
only). Most setups do not need one.

---

← [First campaign](first-campaign.md) · [User guide](README.md) · Next: [Search spaces](search-spaces.md) →
