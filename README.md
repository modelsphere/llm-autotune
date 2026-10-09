# LLM AutoTune

LLM AutoTune finds the best serving configuration for an LLM inference engine
(sglang, vLLM). You declare what to tune and what "better" means; the platform
launches each configuration on real GPUs, benchmarks it, scores it against your
objective, and records exactly what ran.

![LLM AutoTune platform overview](docs/api/diagrams/platform-overview.png)

## Features

- **Campaigns**: a model, a search space, an objective and a nightly window;
  the platform runs the search and ranks the results.
- **Search policies**: enumerate the space, or plug in a search algorithm as a
  container ([policy contract](docs/api/policy-contract.md)). Random search is
  included.
- **Measured by the platform**: every configuration is benchmarked the same
  way through [LLMBench](https://github.com/modelsphere/llm-bench), under your
  latency and quality redlines.
- **GPUs anywhere**: nodes of a Kubernetes cluster, other clusters, or
  bare-metal machines over ssh. Machines shared with production are captured
  and restored around each night.
- **Baselines**: measure what production runs today and tune from it.
- **Reproducible results**: each run records its launch command, engine
  version, card type and dataset.
- **Promotion**: a winner becomes a merge request against your deploy repo.
- **Reports**: an agent API that LLM-written performance reports are built on.
- **Plugins**: add API routes, pages, scheduled steps and search strategies
  ([plugins](docs/plugins.md)).

## Install

You need a Kubernetes cluster with NVIDIA GPU nodes (the device plugin
installed), and `kubectl`, `helm`, `git` and `openssl` on your machine.

```bash
git clone https://github.com/modelsphere/llm-autotune
cd llm-autotune
deploy/quickstart.sh
```

It installs LLM AutoTune and LLMBench into the cluster kubectl points at
(`--context` to pick another), connects them with a generated service key, and
registers the cluster as a machine pool. Then it prints the logins and
port-forwards both UIs:

| | | sign in as |
|---|---|---|
| LLM AutoTune | <http://localhost:8080> | `admin` and the printed password |
| LLMBench | <http://localhost:8081> | `admin@example.com` and the printed password |

On **Resources**, lease `local-cluster` to the platform; then create a search
space and a campaign. Running the script again upgrades the same install.

- [After installing](docs/after-installing.md): your own values, search
  policies, more GPUs (another cluster, ssh machines), ingress, datasets,
  promotion.
- [Deploying](docs/deploying.md): installing the Helm chart yourself, and every
  setting.

## Try it without GPUs

```bash
deploy/demo.sh --kind
```

Creates a local [kind](https://kind.sigs.k8s.io/) cluster (give Docker about
8 GB), installs both platforms there, and starts a demo campaign on a mock
engine that needs no GPU. The whole loop runs for real, but the numbers mean
nothing about performance. `deploy/demo.sh down --kind` removes it.

## The pieces

| | |
|---|---|
| **Campaign** | One search: a model, a search space, an objective, a machine pool and a window. |
| **Search space** | The configurations allowed. The platform enumerates it, or a policy explores it. |
| **Objective** | What "better" means: one target metric plus redlines a config must hold. |
| **Run** | One configuration, launched and measured. |
| **Baseline** | The configuration production runs today, measured the same way. |
| **Machine / cluster** | Where runs land: a node pool in a Kubernetes cluster, or a bare-metal box reached over ssh. |
| **Policy** | A container that decides what to try next. |
| **Promotion** | A winner as a merge request against your deploy repo. |
| **Report** | A written comparison of a baseline against attempts. |

## Repository layout

```text
backend/     FastAPI API + the orchestrator worker (Python 3.12, uv)
  app/
    control/    the control stack: search space, orchestrator, launch drivers
    evaluation/ health checks and the benchmark-platform adapter
    agent/      read models an LLM writes performance reports from
frontend/    Vue 3 + TypeScript SPA
deploy/
  quickstart.sh  install both platforms on your cluster; demo.sh: the no-GPU try-out
  helm/      the chart
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
- [After installing](docs/after-installing.md) — your own values, policies,
  more GPUs, ingress, datasets and promotion
- [Deploying](docs/deploying.md) — the chart, GPU access, the benchmark
  platform, and every other setting
- [Plugins](docs/plugins.md) — extending the platform without forking it
- [Policy contract](docs/api/policy-contract.md) — writing a search that plugs in
- [Machine lease API](docs/api/machine-lease.md) — handing machines to the platform
  from another system
- [Agent API](docs/api/agent-api.md) — the read models reports are written from

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `uv run pytest` in
`backend/`, `npm run typecheck` in `frontend/`, and `helm lint` on the chart.

## License

[Apache License 2.0](LICENSE).
