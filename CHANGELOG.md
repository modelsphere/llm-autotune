# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `AUTOTUNE_LLMBENCH_REPLAY_MODULE` (default `replay`): the name LLMBench's
  traffic replay module reports its metrics under. The metric catalog, the
  default second-stage objective and dataset pinning all follow it, for a
  build of LLMBench that names the module otherwise.
- `POST /api/machines/{id}/smoke-test[?pod=true]`: whether the platform can
  reach a machine at all, by walking the path a launch takes and stopping at
  the first step a launch could not get past. Over ssh: login, the docker
  daemon, and the GPUs `nvidia-smi` sees against those recorded. On
  Kubernetes: the cluster config, the credential, RBAC, the node(s) the
  selector names and the endpoint host, and optionally a throwaway probe pod
  scheduled like the engine's (always deleted). It also warns when a machine
  has no GPU type recorded.
- Plugins: an installed Python package can add API routes, steps in the
  worker's tick, tables with their own migration history, and bootstrap
  seeding, enabled by name with `AUTOTUNE_PLUGINS`. See `docs/plugins.md`;
  `backend/tests/plugins/example` uses every hook, and CI tests it against
  Postgres on every change.

### Plugin API

- Version 1. Hooks: `routers`, `tick_steps`, `migrations`, `on_bootstrap`,
  `propose_candidates` (in-process planning, with a `PlanContext` holding the
  campaign's history with the objective applied), `on_campaign_created` and
  `campaign_extensions` (a plugin's own fields about a campaign, as
  `extensions` on the campaign API, its spec and clones). The importable
  surface is `app.plugin_api`.

### Changed

- Every campaign endpoint that returns a campaign (create, read, list, clone,
  status, schedule, force start and stop) now returns it the same way, with
  its machine warnings and `extensions`.
- CI: every GitHub Action is pinned to a commit SHA, workflows run with a
  read-only token and do not keep it after checkout, and a new push to a pull
  request cancels the runs for the previous one. Dependabot now proposes
  updates for the backend, frontend and operator dependencies and the Actions.
- `NOTICE` lists every third-party component the images ship, with corrected
  licenses (Starlette and Uvicorn are BSD-3-Clause).
- The `policies` submodule points at llm-autotune-policies 5e6f62b: its README
  shows how to register a policy with `POST /api/policies`, and it gains a
  NOTICE and a CHANGELOG. The policies themselves are unchanged.

### Removed

- The CI check that searched the tree for a list of internal host names; that
  list no longer lives in the repository.

## [0.1.2] - 2026-09-29

### Added

- `deploy/quickstart.sh`: one command installs LLM AutoTune next to LLMBench
  0.1.2 on any Kubernetes cluster (or a kind cluster it creates), connects the
  two with a service key, builds the mock engine and loads it into the cluster
  (or pushes it with `--registry`), and starts a demo campaign. No GPUs needed.
  `policy DIR` builds, loads and registers a search policy; `ui` reopens the
  UIs; `down` removes everything. Two `.custom.yaml` files of the user's are
  applied last on every run, so the same install can be taken further.
- `docs/after-the-quickstart.md`: from the demo install to policies, real GPUs
  (in the cluster, another cluster, ssh machines), ingress, datasets,
  promotion and the operator.
- `extraEnv` in the chart, for any platform setting it has no value for;
  `docs/deploying.md` lists the ones installs usually need.
- `values-quickstart.yaml`, for a cluster without GPUs: runs request no cards,
  and a DaemonSet (`mockModel.enabled`) writes the placeholder model directory
  the mock engine needs on every node.
- A second benchmark template, `autotune-quickstart-v1`: a light sweep (short
  requests, concurrency 1 and 4) that the demo screens with. On a laptop the
  default screen measures the benchmark client rather than the mock.
- Four built-in objectives, seeded on install and upgrade. A fresh install had
  none, so the campaign form could not preselect one.

### Changed

- The mock engine is fast by default: 1 s startup, 20 ms to the first token,
  1 ms per token.
- `values-mock.yaml` (the fake LLMBench) now only switches the fake on; layer it
  on `values-quickstart.yaml`.
- With `gpuCluster.inCluster`, `publicApiUrl` defaults to the API's in-cluster
  address, so policy campaigns work on the cluster the platform runs in without
  setting it.

### Fixed

- The worker waits for the database and the schema instead of crash-looping
  until the migrate job has run; `helm install --wait` no longer times out.
  The migrate job waits for Postgres instead of failing its first attempt.
- The screen benchmark is created on LLMBench even when LLMBench comes up after
  AutoTune: the worker retries until it succeeds. On upgrades the install job
  also skipped it, and the `local-cluster` registration, whenever a user
  existed.
- The install job's log shows what it seeded; running migrations had silenced
  it.
- A campaign made active while a worker tick was between planning and
  scheduling was marked done without running anything: the scheduler saw no
  candidates and took the search for finished. It now waits for the next tick
  to plan it. The quickstart's demo, started the moment the worker comes up,
  hit this.
- A policy on a machine with no GPUs could not launch anything (every launch
  was refused as a card-count mismatch), so it reported its space exhausted at
  once. Launches there now ask for no cards.

## [0.1.1] - 2026-09-24

### Changed

- The `TuningRun` API group is now `tuning.modelsphere.dev` (was
  `tuning.llm-autotune.io`), and the operator labels its pods
  `tuning.modelsphere.dev/run`. An operator installed from 0.1.0 has to be
  reinstalled: a CRD cannot be renamed in place, and TuningRuns in the old group
  are not carried over (they are per-run and short-lived). Clusters already
  registered on the platform are moved to the new group by migration
  `002_crd_group`.
- The Deployments, Services and policy Jobs the platform creates itself are
  labelled `autotune.modelsphere.dev/{run,managed,policy}` (were
  `llm-autotune.io/…`). The platform finds its objects by these labels, so stop
  running campaigns before upgrading from 0.1.0; anything left over shows up
  with `kubectl get deploy,svc,job -l llm-autotune.io/managed=true`.

## [0.1.0] - 2026-09-24

### Added

- First public release of LLM AutoTune: campaigns, the launch stack (ssh+docker
  and Kubernetes drivers), search spaces and objectives, config baselines, the
  policy-as-code contract with an SDK and a reference policy, the promotion path,
  the agent API for generated performance reports, and a Helm chart that installs
  the whole platform.
- Images `4pdosc/llm-autotune-backend` and `4pdosc/llm-autotune-frontend` on
  Docker Hub, for amd64 and arm64. The mock engine, the operator and the
  policies are built from source.
- Released under the Apache License 2.0.

### Notes

- The policy SDK and the two reference policies live in
  [llm-autotune-policies](https://github.com/modelsphere/llm-autotune-policies), checked out under `policies/` as a git
  submodule: clone with `--recurse-submodules`.

[Unreleased]: https://github.com/modelsphere/llm-autotune/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/modelsphere/llm-autotune/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/modelsphere/llm-autotune/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/modelsphere/llm-autotune/releases/tag/v0.1.0
