# LLM AutoTune

An inference engine has dozens of knobs, and the right setting for one is not the
right setting for the next model, the next card, or the next traffic shape.
Finding them by hand costs GPU nights and produces numbers nobody can reproduce.

LLM AutoTune runs that search as an experiment: you declare what to tune and what
"better" means, and the platform launches each configuration on real GPUs,
benchmarks it, scores it against your objective, and records exactly what ran.

![LLM AutoTune platform overview](docs/api/diagrams/platform-overview.png)

**What makes it different from a sweep script:**

- **The measurement is the platform's, not the search's.** A search algorithm
  never reports its own score. It proposes a configuration; the platform launches
  it, benchmarks it and judges it. That is what makes two runs comparable.
- **Search is a container, not a plugin.** A smarter search is a program that
  talks to an HTTP contract, in any language, versioned on its own. See
  [policy-contract.md](docs/api/policy-contract.md).
- **A borrowed machine is given back.** On a box shared with production, the
  platform captures what is running, verifies it can put it back, tears it down,
  runs the night, and restores it — and refuses to proceed when it cannot.
- **A result knows what produced it.** Every run records its exact launch
  command, engine version, card type and dataset, so a number six months old is
  still worth something.

## Quickstart — both platforms, no GPUs

The fastest way to understand the platform is to watch it run. One script
installs LLM AutoTune next to [LLMBench](https://github.com/modelsphere/llm-bench),
the benchmark platform that measures every run, connects the two, and starts a
demo campaign. The campaign tunes a **mock engine**: an image that answers the
same endpoints sglang does, with no GPU and no weights. Everything around it is
real: scheduling, launching, health checks, benchmarking with guidellm, and
ranking.

You need `kubectl`, `helm`, `docker`, `git` and `openssl`, and either
[kind](https://kind.sigs.k8s.io/) (the script creates the cluster; give Docker
about 8 GB of memory) or a Kubernetes cluster your current kubectl context
points at:

```bash
git clone https://github.com/modelsphere/llm-autotune
cd llm-autotune
deploy/quickstart.sh --kind      # or, on the cluster kubectl points at: deploy/quickstart.sh
```

It builds the mock engine from `mock-engine/` and loads it into the cluster
(kind, minikube, k3d, Docker Desktop and OrbStack take it directly; for any
other cluster add `--registry <repo>`, one you can push to and its nodes can
pull from). It takes a few minutes, most of them pulling images, then prints
the logins and port-forwards both UIs until you press Ctrl-C:

| | | sign in as |
|---|---|---|
| LLM AutoTune | <http://localhost:8080> | `admin` and the printed password |
| LLMBench | <http://localhost:8081> | `admin@example.com` and the printed password |

In LLM AutoTune, open **Campaigns ▸ Quickstart: mock engine**. It tries the mock
at two speeds. Each run goes `pending → launching → waiting_ready →
health_check → benching → succeeded` in a minute or two, and the leaderboard
ranks the faster one first. Every run is also a submission on LLMBench, with
its sweep results. Nothing measured this way means anything about performance;
it proves the machinery, which is the point.

- `deploy/quickstart.sh ui` opens the UIs again, and `deploy/quickstart.sh down`
  removes both platforms and their data (`down --kind` also deletes the kind
  cluster). Running the script again upgrades the same install; its passwords
  and keys are in `.quickstart/secrets.env`, and your own values for either
  release go in `.quickstart/llm-autotune.custom.yaml` and
  `.quickstart/llm-bench.custom.yaml`.
- To make a campaign of your own, first add a search space on **Search spaces**
  (engine `sglang`, a grid over the mock's knobs such as `mock_token_ms` or
  `mock_latency_ms`; see [mock-engine/](mock-engine/README.md)). Then, under
  **New campaign**, use image `llm-autotune-mock-engine:0.1.2` (the one the
  script built), model path `/var/lib/llm-autotune/mock-model`, machine `local-cluster`, and
  benchmark `autotune-quickstart-v1` (the default screen is sized for real
  engines, and on a laptop it measures the benchmark client instead of the
  mock). A campaign runs in its nightly window; **Force start** runs it now.

**What the script does**, if you would rather do it by hand or adapt it
([deploy/quickstart.sh](deploy/quickstart.sh)):

1. Generates the secrets both platforms need, among them one service key
   (`llmb_…`) that LLMBench is installed with and AutoTune authenticates with.
2. Builds the mock engine image and loads it into the cluster (or pushes it).
3. Installs LLMBench from its chart (the release this version was tested with),
   which creates a `service` account holding that key.
4. Installs AutoTune with `-f deploy/helm/llm-autotune/values-quickstart.yaml`
   and `llmbench.url` / `llmbench.apiKey` pointing at LLMBench. The values file
   is for a cluster without GPUs: runs request no cards, and a DaemonSet puts a
   placeholder model directory on every node for the mock.
5. Through AutoTune's API, leases the `local-cluster` machine to the platform,
   then creates and force-starts the demo campaign.

### Next steps

[docs/after-the-quickstart.md](docs/after-the-quickstart.md) takes the same
install further, one piece at a time:

- **Search policies**: `deploy/quickstart.sh policy policies/random-search`
  builds a policy, loads it into the cluster and registers it; then pick it as
  a campaign's Strategy. The same command installs your own.
- **Real GPUs**: GPU nodes in this cluster, another cluster, or bare-metal
  machines over ssh.
- **Any setting** of either release, through the two `.custom.yaml` files:
  ingress, datasets for LLMBench, promotion to GitLab, the operator, and every
  platform setting.

## Installing it for real

Install LLMBench from its own chart with a service key
(`scripts/gen-prod-secrets.sh --service-key` makes one; see LLMBench's
[deploying guide](https://github.com/modelsphere/llm-bench/blob/main/docs/deploying.md#connecting-llm-autotune)),
and AutoTune with the same key. Either can go first: AutoTune keeps trying to
reach LLMBench until it can.

```bash
helm install autotune deploy/helm/llm-autotune \
  --set jwtSecret=$(openssl rand -hex 32) \
  --set adminPassword=... \
  --set llmbench.url=http://llm-bench-backend.llm-bench:8000 \
  --set llmbench.apiKey=llmb_... \
  --set gpuCluster.inCluster=true
```

[docs/deploying.md](docs/deploying.md) covers the values that matter: where GPUs
come from (this cluster, another cluster, or bare-metal boxes over ssh), how the
benchmark platform is reached, and what a policy container needs to call home.

## The pieces

| | |
|---|---|
| **Campaign** | One search: a model, a search space, an objective, a machine pool and a window. |
| **Search space** | The configurations allowed. The platform enumerates it, or a policy explores it. |
| **Objective** | What "better" means: one target metric plus redlines a config must hold. |
| **Run** | One configuration, launched and measured. The unit everything else is built from. |
| **Baseline** | The configuration production actually runs, measured the same way, so a win is a win against something real. |
| **Machine / cluster** | Where runs land: a node pool in a Kubernetes cluster, or a bare-metal box reached over ssh. |
| **Policy** | A container that decides what to try next, over an HTTP contract. |
| **Promotion** | A winner as a change request against the file production is deployed from. |
| **Report** | A written comparison of a baseline against attempts, generated from the runs. |

## Where runs land

The launch layer is a **per-machine driver**, so a fleet can mix substrates while
it migrates:

- **`k8s`** — a slice of a GPU cluster. The driver never picks nodes or devices:
  it asks for a GPU *count* and lets the scheduler place the pod. It renders
  either a plain Deployment plus a NodePort Service, which works on any cluster,
  or a `TuningRun` for the [operator](operator/) to reconcile.
- **`ssh_docker`** — a bare-metal box the platform borrows for the night. The
  worker ssh'es in and runs the engine under `docker run`. Because such a box is
  usually shared with production, the capture-and-restore interlock above is not
  optional here; it is the reason the driver exists in this shape.

## Repository layout

```text
backend/     FastAPI API + the orchestrator worker (Python 3.12, uv)
  app/
    control/    the control stack: search space, orchestrator, launch drivers
    evaluation/ health checks and the benchmark-platform adapter
    agent/      read models an LLM writes performance reports from
frontend/    Vue 3 + TypeScript SPA
deploy/
  helm/      the chart — the supported way to install the platform
  docker/    images, and a compose file for local development
  k8s/       RBAC for a GPU cluster the platform does not run inside
policies/    submodule: llm-autotune-policies — the SDK and two policies
operator/    the optional Kubernetes operator (Go)
mock-engine/ a fake engine, for running the loop without GPUs
docs/        architecture, the API contracts, deployment
```

## Documentation

- [Architecture](docs/architecture.md) — what the system is and the few choices
  that decide the rest. 中文：[架构概览](docs/architecture.zh.md)
- [How it works](docs/workflow.md) — the product walkthrough, no code.
  中文：[产品视角](docs/workflow.zh.md)
- [After the quickstart](docs/after-the-quickstart.md) — policies, real GPUs
  and your own settings on the quickstart install
- [Deploying](docs/deploying.md) — the chart, GPU access, the benchmark
  platform, and every other setting
- [Policy contract](docs/api/policy-contract.md) — writing a search that plugs in
- [Machine lease API](docs/api/machine-lease.md) — handing machines to the platform
  from another system
- [Agent API](docs/api/agent-api.md) — the read models reports are written from

## What you need to run it for real

- A Kubernetes cluster for the platform itself (it is small: an API, a worker, a
  UI and Postgres).
- GPUs, as a node pool in a cluster or as machines reachable over ssh.
- A benchmark platform to measure runs. The adapter targets LLMBench; the
  `Evaluator` interface in `backend/app/evaluation/base.py` is the seam if you
  measure some other way.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `uv run pytest` in
`backend/`, `npm run typecheck` in `frontend/`, and `helm lint` on the chart.

## License

[Apache License 2.0](LICENSE).
