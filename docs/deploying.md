# Deploying LLM AutoTune

One Helm chart deploys the API, the orchestrator worker, the UI and
Postgres. Most values follow from two questions: where the GPUs are, and what
benchmarks the runs.

[deploy/quickstart.sh](../deploy/quickstart.sh) installs the chart next to
LLMBench with these values set ([README](../README.md#install)). This page
covers installing the chart directly.

```bash
helm install autotune deploy/helm/llm-autotune
```

No value is required. `jwtSecret`, `adminPassword` and the Postgres password
are generated on install when not given, and kept on every upgrade, in the
release's Secret (`llm-autotune-secrets` for a release named `llm-autotune`). That Secret is kept on
`helm uninstall`, like the database volume, so a reinstall opens the same
data; delete both to start over. `jwtSecret` signs sign-in tokens and encrypts
stored GPU cluster kubeconfigs, so rotating it signs everyone out and makes
stored kubeconfigs unreadable.

`adminPassword` is the first account's, created only while the users table is
empty; the chart never overwrites a changed password. The install notes print
the command that reads a generated one.

`allowSignup` (default `true`) lets anyone who can reach the UI create an
ordinary account on the sign-in page. Set it to `false` to have admins add
users on the Account page.

## GPU access

The platform's own cluster needs no GPUs. GPUs come from the clusters and
machines added on the Resources page.

### GPU clusters

Prepare each GPU cluster with its admin kubeconfig:

```bash
deploy/gpu-cluster.sh            # on the GPU cluster's context; --namespace NS
```

It creates a namespace for engine and policy pods, a ServiceAccount allowed
only to manage those (Deployments, Services, Jobs, TuningRuns) and read their
pods, logs and events, read-only access to nodes, and a kubeconfig carrying
only that account's token. It never grants write access to nodes: no cordon,
label or taint. With `--count-free-cards` it may also list pods in every
namespace, so the platform can show how many cards other workloads leave free;
a pod listing includes other teams' pod specs, so it is off by default. On
**Resources ▸ Add GPU cluster**, upload the kubeconfig (encrypted at rest with
`jwtSecret`) and register each GPU type as a pool, or each node as its own
machine ([Node pools](guide/machines.md#node-pools)).

The GPU cluster's API server, and its nodes' NodePorts, must be reachable from
this cluster, and the GPU cluster must reach `publicApiUrl` for policy
campaigns.

### The platform cluster's own GPUs

```yaml
gpuCluster:
  inCluster: true
  namespace: ""          # empty = the release's own namespace
```

For GPUs in the cluster the platform runs in, without a kubeconfig: the chart
grants its own ServiceAccount the same access and, on an empty install,
registers the cluster on **Resources ▸ Clusters** (named `gpuCluster.name`,
`local`), with one machine called `local-cluster`: a pool of every node, the
scheduler placing each pod. A pool holds one GPU type, so on a cluster with
several, set `gpuCluster.autoRegisterMachine=false` and register a pool per
type from **Clusters ▸ Nodes**, or give the machine a selector such as
`nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3`.

The `gpuCluster` values describe this one cluster: they are copied onto its
row once, and edited there afterwards. A GPU cluster added later from
Resources carries its own settings; it is reached over a NodePort, not a
Service name, and its policies need `publicApiUrl`.
`gpuCluster.countFreeCards=true` lets it count the cards other workloads leave
free (it reads pods in every namespace). `deploy/quickstart.sh` does not use this
mode; the demo does.

A cluster without GPUs (kind, a CPU-only test cluster) takes
`-f deploy/helm/llm-autotune/values-demo.yaml`: runs then request no
cards, and campaigns run the mock engine
([mock-engine/](../mock-engine/README.md)).

### Bare-metal machines over ssh

```yaml
worker:
  ssh:
    enabled: true
    existingSecret: autotune-worker-ssh
```

Create the Secret separately; a private key does not belong in a values file:

```bash
kubectl create secret generic autotune-worker-ssh \
  --from-file=id_ed25519=$HOME/.ssh/id_ed25519
```

Then add each machine on the Resources page and lease it. Lease a machine
with nothing else running on its GPUs; when the lease ends, the platform stops
only the containers it started.

## Benchmark platform

Every run is benchmarked by an external platform; the adapter targets
[LLMBench](https://github.com/modelsphere/llm-bench), installed from its own chart.
The two are wired by one shared secret: a **service account** on LLMBench that
AutoTune acts as. It can submit, read and trigger rolling-dataset builds, and
create and change only the benchmarks and dataset collection profiles it
created itself. It is never an admin.

```bash
# 1. In a clone of llm-bench: secrets for LLMBench, including the service key
scripts/gen-prod-secrets.sh --service-key --out secrets.prod.yaml   # prints the key

# 2. LLMBench seeds the service account with it
helm upgrade --install llm-bench deploy/helm/llm-bench -n llm-bench --create-namespace \
  -f secrets.prod.yaml

# 3. In this repository: AutoTune authenticates with the same value
helm upgrade --install llm-autotune deploy/helm/llm-autotune -n llm-autotune --create-namespace \
  --set llmbench.url=http://llm-bench-backend.llm-bench:8000 \
  --set llmbench.apiKey=llmb_...
```

Installed in the same namespace, AutoTune can read the key from LLMBench's own
Secret instead of a copy: `--set llmbench.apiKeySecret.name=llm-bench` (key
`SERVICE_API_KEY`). The ModelSphere stack installs the two that way.

```yaml
llmbench:
  url: http://llm-bench-backend.llm-bench:8000
  webUrl: https://llmbench.example.com   # where a person opens a submission; default: url
  apiKey: ""            # the service key above (or email + password for a user account)
  benchmarkSlug: autotune-screen-v1   # created and locked on LLMBench at startup
```

AutoTune creates its screening benchmark on LLMBench from
`backend/app/evaluation/benchmark_templates/autotune-screen-v1.yaml` and locks
it, so every result in a campaign is measured with the same thing. It tries at
install and then keeps trying from the worker until it succeeds, so the order
the two platforms are installed in does not matter; until then the worker log
says why it could not. If that slug already exists and another account created
it, AutoTune refuses to adopt it; choose another slug. An admin can also run
this with `POST /api/benchmarks/ensure`.

A campaign usually describes its workload instead of naming a benchmark
(synthetic prompts of a given size, or a replay of a dataset), and AutoTune
creates the matching benchmark when the campaign is created. The slug is a hash
of the workload, so the same workload reuses one benchmark. Everything AutoTune
creates on LLMBench is filed under the group tag `llm-autotune`. The
full contract between the two platforms (routes, roles, metric names, dataset
stamping) is in llm-bench's `docs/api/for-autotune.md`.

**Benchmark slots.** LLMBench runs as many benchmarks at once as it has worker
slots: worker pods × `app.worker.processes` × `app.worker.threads`. AutoTune
starts one benchmark per trial in flight, and a campaign's validation runs its
finalists and production side by side. A benchmark that finds no free slot
waits in LLMBench's queue while its engine holds its GPUs; AutoTune's service
account skips LLMBench's per-user cap, but not this queue. So size the slots to
the most benchmarks your campaigns run at once: for a campaign, about its GPU
budget ÷ the cards each config uses, more during validation. The quickstart
installs 32 slots (4 pods × 8 processes, 16 Gi each); see llm-bench's
`docs/deploying.md#sizing` to change it.

`llmbench.webUrl` is where a person's browser reaches LLMBench; the run pages
link there. It defaults to `llmbench.url`, which is usually a cluster-internal
address, so set it to LLMBench's ingress.

Replay datasets ([Datasets](guide/datasets.md)) are collected by LLMBench's
dataset collector from a VictoriaLogs store of the gateway's request logs.
Turn the collector on in LLMBench's values (`datasets.enabled` and
`datasetBuilder.enabled`; the datasets volume is shared by LLMBench's
backend, workers and collector, so a multi-node cluster needs ReadWriteMany
storage). AutoTune's dataset form starts from the store's address as
LLMBench's pods reach it. The default, `http://victorialogs.logging:9428`, is
where the bodylog-victorialogs chart
([modelsphere/helm-charts](https://github.com/modelsphere/helm-charts)) serves
it when installed into the `logging` namespace, as its README does. Set your
own if yours is elsewhere:

```yaml
llmbench:
  victoriaLogsUrl: http://victorialogs.my-namespace:9428
```

Without a benchmark platform, runs launch and then have nothing to measure
them. To work on AutoTune without one, layer `values-mock.yaml` on
`values-demo.yaml`: it replaces LLMBench with a fake that probes the
endpoint and reports invented numbers.

If you measure some other way, `Evaluator` in `backend/app/evaluation/base.py` is
a two-method interface (`start`, `poll`) to implement.

## Public addresses

```yaml
publicApiUrl: https://autotune.example.com   # how a POLICY CONTAINER calls home
publicUiUrl:  https://autotune.example.com   # how a PERSON reaches the UI
```

Both must be reachable from outside the platform's pods. A policy container
runs on a GPU cluster and calls the platform to request launches and read
results. On the platform's own cluster it uses the API's Service name, which
the chart always sets; on any other cluster or ssh machine it uses
`publicApiUrl`, and a policy campaign there fails at start without one.

## The operator

```yaml
operator:
  enabled: true
```

Optional. Without it, a run is a Deployment plus a NodePort Service that the
platform manages itself, which works on any cluster. With it, a run is a
`TuningRun` that the [operator](../operator/) reconciles. Install the operator
separately ([operator/INSTALL.md](../operator/INSTALL.md)); the flag only
selects which object the platform creates.

## Storage

`runLogs` (on by default) keeps complete engine logs as files: the worker
writes them and the API serves them, so the claim needs a `ReadWriteMany`
storage class. On a cluster without one, set `runLogs.enabled=false`. The UI
still shows logs: the end of every engine and policy log is stored in the
database, and a running container's log is read from the cluster.

The install checks the class the claim would get. When it only makes
single-node volumes (local-path, hostpath, a cloud block disk such as EBS or a
GCE persistent disk), when the cluster has no default class, or when the named
class does not exist, the install notes say so: the claim would never bind,
and the API and worker would stay Pending. `helm install --wait` prints no
notes; it times out instead, with the API and worker Pending and the
`<release>-runlogs` claim unbound.

While a container runs, the worker saves the end of its log every two minutes,
each save replacing the last, so a pod that is evicted or lost before its run
ends still leaves its last lines. A saved log is the last 2,000 lines, at most
1 MB (`AUTOTUNE_LOG_SNAPSHOT_SECONDS`, `AUTOTUNE_LOG_LINES`,
`AUTOTUNE_LOG_FILE_MAX_BYTES`).

Postgres is a StatefulSet by default. To use a database you already run:

```yaml
postgresql:
  enabled: false
externalDatabase:
  asyncUrl: postgresql+asyncpg://user:pass@host:5432/autotune
  syncUrl:  postgresql+psycopg2://user:pass@host:5432/autotune
```

Both DSNs, because the API is async and the worker and migrations are not.

## Other settings

Everything the platform reads is an `AUTOTUNE_*` environment variable
(`backend/app/core/config.py`, each with a comment). The chart sets the ones
its values cover; any other goes in `extraEnv`, as plain k8s env entries for
the API, the worker and the migrate job:

```yaml
extraEnv:
  - {name: AUTOTUNE_DEFAULT_DAILY_START, value: "23:00"}
  - name: AUTOTUNE_LLMBENCH_API_KEY
    valueFrom: {secretKeyRef: {name: autotune-llmbench, key: api-key}}
```

The ones installs usually reach for:

| variable | default | what it does |
|---|---|---|
| `AUTOTUNE_ALLOW_SIGNUP` | `true` | whether the sign-in page offers **Create account** (the chart's `allowSignup`) |
| `AUTOTUNE_DEFAULT_DAILY_START`, `_END`, `AUTOTUNE_DEFAULT_SCHEDULE_TIMEZONE` | — | the window a drafted campaign gets when none is given |
| `AUTOTUNE_DEFAULT_MAX_RUN_MINUTES` | `240` | window a run reserves when its length can be neither measured nor estimated from its benchmark |
| `AUTOTUNE_POLICY_START_TIMEOUT_MINUTES` | `30` | how long a policy container may take to send its first heartbeat (how long an engine may take to load is the campaign's **Model load limit**) |
| `AUTOTUNE_IMAGE_PULL_TIMEOUT_MINUTES` | `45` | how long a pod may spend scheduling and pulling its image, and how long an ssh machine may spend pulling one |
| `AUTOTUNE_HEALTH_PROBE_TIMEOUT_SECONDS` | `600` | how long the first chat completion may take |
| `AUTOTUNE_K8S_IMAGE_PULL_SECRETS` | — | pull secrets (comma-separated names) for engine and policy pods |
| `AUTOTUNE_K8S_TOLERATIONS` | — | taints engine pods tolerate, e.g. `dedicated=ml:NoSchedule` |
| `AUTOTUNE_K8S_ENGINE_CPU_REQUEST`, `_MEMORY_REQUEST`, `_CPU_LIMIT`, `_MEMORY_LIMIT` | — | engine container resources, for namespaces whose quota requires them |
| `AUTOTUNE_LLMBENCH_REPLAY_MODULE` | `replay` | the name LLMBench's traffic replay module reports metrics under; change it only for a build of LLMBench that names it otherwise |
| `AUTOTUNE_K8S_POLICY_PRIORITY_CLASS` | — | a PriorityClass for policy pods |
| `AUTOTUNE_AGENT_API_ENABLED` | `false` | serve the agent API (`/api/agent/v1`); the Reports pages also need the frontend built with `VITE_AGENT_REPORTS=1` |
| `AUTOTUNE_REPORT_NOISE_THRESHOLD_PCT` | `1.0` | below this, a difference from the baseline is reported as noise |

## Upgrading

`helm install` runs the schema bootstrap as a Job once the release is applied;
it also seeds the first admin and the built-in objectives. The worker waits for
the schema before it starts (its log says so), so `helm install --wait` works.
`helm upgrade` migrates the schema before it replaces any pod, and drops no
requests while it rolls. See [upgrading.md](upgrading.md).

## Local development

The chart is the supported install. Running the code outside it, for
development, is covered in [CONTRIBUTING.md](../CONTRIBUTING.md#running-it-locally).
