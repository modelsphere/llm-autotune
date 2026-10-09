# Deploying LLM AutoTune

One Helm chart brings up everything: the API, the orchestrator worker, the UI
and Postgres. Two questions decide the rest of the values — where the GPUs
are, and what measures a run.

[deploy/quickstart.sh](../deploy/quickstart.sh) installs it next to LLMBench
with these choices made for you (see the [README](../README.md#install)). This
page is for installing the chart yourself.

```bash
helm install autotune deploy/helm/llm-autotune \
  --set jwtSecret=$(openssl rand -hex 32) \
  --set adminPassword=...
```

`jwtSecret` is the only required value. It signs sign-in tokens **and** encrypts
the kubeconfigs you paste in for GPU clusters, so rotating it logs everyone out
and makes stored kubeconfigs unreadable. Generate it once and keep it.

`adminPassword` creates the first account, and only ever when the users table is
empty — the chart never overwrites a password someone has changed. Leave it out
and no account is created, which means nothing can be done in the UI until you
create one another way.

## Where the GPUs are

### This cluster

```yaml
gpuCluster:
  inCluster: true
  namespace: ""          # empty = the release's own namespace
```

The chart grants its ServiceAccount exactly what the launch driver needs in that
namespace — create and read TuningRuns or Deployments, read pods, pod logs and
warning events, run policy Jobs — plus read-only access to nodes, which is
cluster-scoped and is how the platform fills in a machine's card count and type
without being told. It never writes a node: no cordon, no label, no taint.

On an empty install it also registers that cluster as a machine called
`local-cluster`, so the Resources page has something in it. Use *Refresh
capacity* there to fill in its GPUs, then **Lease to platform** to let campaigns
use it. Set `gpuCluster.autoRegister=false` to skip this.

A cluster without GPUs (kind, a CPU-only test cluster) takes
`-f deploy/helm/llm-autotune/values-demo.yaml`: runs then request no
cards, and campaigns run the mock engine
([mock-engine/](../mock-engine/README.md)).

### A different cluster

Leave `gpuCluster.inCluster` false and add the cluster from the Resources page by
pasting a kubeconfig. Mint a *scoped* one rather than handing over an admin
credential:

```bash
kubectl apply -f deploy/k8s/remote-cluster/backend-rbac.yaml   # on the GPU cluster
deploy/k8s/remote-cluster/make-scoped-kubeconfig.sh
```

The kubeconfig is encrypted at rest with `jwtSecret`.

### Bare-metal boxes over ssh

```yaml
worker:
  ssh:
    enabled: true
    existingSecret: autotune-worker-ssh
```

Create the Secret yourself — a private key does not belong in a values file:

```bash
kubectl create secret generic autotune-worker-ssh \
  --from-file=id_ed25519=$HOME/.ssh/id_ed25519
```

Then add each machine from the Resources page. Such a box is usually shared with
production, so the platform captures what is already running on it, verifies it
can put it back, clears it for the night, and restores it afterwards. It refuses
to run experiments on a machine it could not capture.

## What measures a run

Every run is benchmarked by an external platform; the adapter targets
[LLMBench](https://github.com/modelsphere/llm-bench), installed from its own chart.
The two are wired by one shared secret: a **service account** on LLMBench that
AutoTune acts as. It can submit, read and trigger rolling-dataset builds, and
create and change only the benchmarks it created itself. It is never an admin.

```bash
# 1. In a clone of llm-bench: secrets for LLMBench, including the service key
scripts/gen-prod-secrets.sh --service-key --out secrets.prod.yaml   # prints the key

# 2. LLMBench seeds the service account with it
helm upgrade --install llm-bench deploy/helm/llm-bench -n llm-bench --create-namespace \
  -f secrets.prod.yaml

# 3. In this repository: AutoTune authenticates with the same value
helm upgrade --install llm-autotune deploy/helm/llm-autotune -n llm-autotune --create-namespace \
  --set jwtSecret=$(openssl rand -hex 32) --set adminPassword=... \
  --set llmbench.url=http://llm-bench-backend.llm-bench:8000 \
  --set llmbench.apiKey=llmb_...
```

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

`llmbench.webUrl` is where a person's browser reaches LLMBench; the run pages
link there. It defaults to `llmbench.url`, which is usually a cluster-internal
address, so set it to LLMBench's ingress.

Without a benchmark platform, runs launch and then have nothing to measure
them. To work on AutoTune without one, layer `values-mock.yaml` on
`values-demo.yaml`: it replaces LLMBench with a fake that probes the
endpoint and reports invented numbers.

If you measure some other way, `Evaluator` in `backend/app/evaluation/base.py` is
a two-method interface (`start`, `poll`) and the seam to implement.

## Addresses other systems use

```yaml
publicApiUrl: https://autotune.example.com   # how a POLICY CONTAINER calls home
publicUiUrl:  https://autotune.example.com   # how a PERSON reaches the UI
```

Both must be reachable from outside the platform's own pods, because that is the
whole point of them. `publicApiUrl` is required for policy campaigns: a policy
container runs on a GPU machine and dials the platform to ask for launches and
read results. Without it, the container has no way back. With
`gpuCluster.inCluster`, it defaults to the API's in-cluster address, which a
policy running in the same cluster can reach; set it when policies run
anywhere else.

## The operator

```yaml
operator:
  enabled: true
```

Optional. Without it, a run is a Deployment plus a NodePort Service that the
platform manages itself — which works on any cluster. With it, a run is a
`TuningRun` the [operator](../operator/) reconciles, and the platform holds no
Kubernetes machinery of its own beyond creating that object. Install the operator
separately (see [operator/INSTALL.md](../operator/INSTALL.md)); the flag only
tells the platform which object to render.

## Storage

Engine logs are written by the worker and served by the API — two pods, one
volume, so the claim asks for `ReadWriteMany`. On a cluster with no RWX storage
class, set `runLogs.enabled=false`: everything works except reading a run's
engine log from the UI.

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
  - name: AUTOTUNE_GITLAB_TOKEN
    valueFrom: {secretKeyRef: {name: autotune-gitlab, key: token}}
```

The ones installs usually reach for:

| variable | default | what it does |
|---|---|---|
| `AUTOTUNE_PROMOTION_TARGET` | `manual` | `gitlab` opens a merge request for a winner instead of only rendering its config |
| `AUTOTUNE_PROMOTION_DRY_RUN` | `true` | build and record the merge request without pushing anything |
| `AUTOTUNE_GITLAB_BASE_URL`, `_PROJECT`, `_TOKEN` | — | the deploy repository promotion writes to; the token needs to push branches and open merge requests |
| `AUTOTUNE_AUTO_RESTORE_PRODUCTION` | `false` | put production back on a borrowed ssh machine when the window or lease ends |
| `AUTOTUNE_AUTO_BASELINE_LIFECYCLE` | `true` | let the worker take production down after a passing canary; `false` asks a person on the Resources page |
| `AUTOTUNE_DEFAULT_DAILY_START`, `_END`, `AUTOTUNE_DEFAULT_SCHEDULE_TIMEZONE` | — | the window a drafted campaign gets when its author names none |
| `AUTOTUNE_DEFAULT_MAX_RUN_MINUTES` | `150` | how long one run may take, start to verdict |
| `AUTOTUNE_READY_TIMEOUT_MINUTES` | `30` | how long a started engine may take to answer `/v1/models` |
| `AUTOTUNE_IMAGE_PULL_TIMEOUT_MINUTES` | `45` | how long a pod may spend scheduling and pulling its image |
| `AUTOTUNE_HEALTH_PROBE_TIMEOUT_SECONDS` | `600` | how long the first chat completion may take |
| `AUTOTUNE_K8S_IMAGE_PULL_SECRETS` | — | pull secrets (comma-separated names) for engine and policy pods |
| `AUTOTUNE_K8S_TOLERATIONS` | — | taints engine pods tolerate, e.g. `dedicated=ml:NoSchedule` |
| `AUTOTUNE_K8S_ENGINE_CPU_REQUEST`, `_MEMORY_REQUEST`, `_CPU_LIMIT`, `_MEMORY_LIMIT` | — | engine container resources, for namespaces whose quota requires them |
| `AUTOTUNE_LLMBENCH_REPLAY_MODULE` | `replay` | the name LLMBench's traffic replay module reports metrics under; change it only for a build of LLMBench that names it otherwise |
| `AUTOTUNE_K8S_POLICY_PRIORITY_CLASS` | — | a PriorityClass for policy pods |
| `AUTOTUNE_REPORT_NOISE_THRESHOLD_PCT` | `1.0` | below this, a difference from the baseline is reported as noise |

## Upgrading

`helm install` and `helm upgrade` run the schema bootstrap as a Job once the
release is applied; it also seeds the first admin and the built-in objectives.
The worker waits for the schema before it starts (its log says so), so
`helm install --wait` works. See [upgrading.md](upgrading.md).

## Local development

The chart is the supported install. Running the code outside it, for working
on the platform, is in [CONTRIBUTING.md](../CONTRIBUTING.md#running-it-locally).
