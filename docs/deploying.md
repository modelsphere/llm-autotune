# Deploying LLM AutoTune

One Helm chart brings up everything: the API, the orchestrator worker, the UI,
Postgres and Redis. Two questions decide the rest of the values — where the GPUs
are, and what measures a run.

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
`local-cluster`, so the Resources page has something in it. Probe it from that
page to fill in its GPUs, then mark it available. Set
`gpuCluster.autoRegister=false` to skip this.

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

Every run is benchmarked by an external platform; the adapter targets LLMBench.

```yaml
llmbench:
  url: http://llmbench.your-cluster:8000
  apiKey: ""            # a personal key authenticates directly
  benchmarkSlug: perf-suite-v1
```

Without it, runs launch and then have nothing to measure them. To try the
platform before you have one, use the mock: `-f values-mock.yaml` replaces
LLMBench with a fake that probes the endpoint and reports invented numbers.

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
read results. Without it, the container has no way back.

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

## Upgrading

`helm upgrade` runs the schema bootstrap as a hook before the new API and worker
start, so neither ever runs against a schema it does not understand. See
[upgrading.md](upgrading.md).

## Local development

The chart is the supported install. For working on the code:

```bash
docker compose -f deploy/docker/docker-compose.dev.yml up -d   # postgres + redis
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 28100
uv run python -m app.worker
cd ../frontend && npm install && npm run dev
```
