# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-10-10

### Added

- The install registers the `random-search` policy from its published image,
  `ghcr.io/modelsphere/llm-autotune-policy-random-search:0.1.1`
  (`POLICY_IMAGE` to choose another), unless a policy of that name exists.
  `quickstart.sh policy` still builds one from source.
- `deploy/dev/mock-gpu-cluster.sh`: a kind cluster that looks like a GPU
  cluster (mock GPUs on each node, an `nvidia` RuntimeClass, the mock engine
  and a placeholder model), so the whole install, from `quickstart.sh` to a
  policy campaign, runs on a laptop with two kind clusters
  ([installing](docs/install.md#trying-it-all-on-a-laptop)).

### Fixed

- On a cluster on this machine (kind, k3d, Docker Desktop), the UIs are still
  port-forwarded, but the API also gets the AutoTune UI's NodePort on a node's
  address as `publicApiUrl`, so a policy on another local cluster can call it.
- `gpu-cluster.sh` on a kind or k3d cluster writes the API server's address on
  the Docker network instead of its loopback port, which no pod reaches, and
  checks the written account through the address this machine uses.
- Creating a kind cluster (`deploy/demo.sh --kind`, `mock-gpu-cluster.sh`)
  leaves out a proxy on this machine's loopback, which kind would otherwise
  hand to the nodes and break every image pull.
- On a kind or k3d cluster the install pulls the charts' images through Docker
  on this machine, with retries, and loads them onto the nodes, which pull
  without Docker's proxy or mirrors and start empty each time they are
  recreated. It imports only this machine's platform when `kind load` cannot
  (Docker's containerd image store).
- Preflight on Kubernetes read the probe pod's status alone, which the kubelet
  does not refresh while it pulls the next image: an engine image already on
  the node read as "still being pulled". The container events decide now.

## [0.2.0] - 2026-10-10

Released with LLMBench 0.2.0, which the install script now installs.

### Added

- A policy campaign's page shows its policy: where the session is (starting,
  searching, finalizing, validating, done), trials and finalists, when it
  last reported, what a starting policy is waiting on, its error, its log, and
  Abort. Session responses carry `finalize_requested_at`,
  `abort_requested_at` and, while starting, `waiting`.
- **Add GPU cluster** on Resources: upload a kubeconfig, and the platform lists
  the cluster's GPU nodes and registers the ones you pick, each as a machine
  pinned to its node with the cards it reports (`GET` and `POST
  /api/clusters/{id}/nodes`). A cluster's **Nodes** registers nodes added
  later and lists registered ones that left. The namespace is read from the
  kubeconfig.
- `deploy/gpu-cluster.sh` prepares a GPU cluster: a namespace for engine pods,
  an account allowed only what the platform needs there plus reading nodes,
  and the kubeconfig to upload. Every name derives from the namespace, so two
  platforms can share a GPU cluster; `remove` revokes the account.
  An API server name this machine knows only from `/etc/hosts`, which the
  platform's pods cannot resolve, is written as its address, with the
  certificate still checked against the name (`tls-server-name`);
  `--api-server` chooses the address. Adding a cluster the platform cannot
  resolve or reach says so and what to change.
- Preflight on a Kubernetes machine: a probe Job on the machine's node pulls
  the engine image and the campaign's policy image, and mounts the model the
  way a run does, so a missing tag, a registry the nodes cannot reach or a
  model path that is not on the node is found before the campaign starts. A
  pull still going when the check ends carries on, so the image is cached for
  the first run. Over ssh, the policy image is checked too.
- A **Policies** page under Automatic Tuning: register a policy image by name,
  edit it, and remove one no campaign uses. The New campaign form links to it
  from its Strategy picker.

- A campaign describes its workload instead of naming a benchmark: synthetic
  prompts (sizes, concurrency levels, requests per concurrent slot) or a replay
  of a dataset. A synthetic level c sends c × `requests_per_concurrency`
  requests (default 20), capped by `max_seconds_per_level`, so every level is
  measured on the same sample however fast the engine is. AutoTune
  creates the matching benchmark on LLMBench when the campaign is created,
  locked, with a slug hashed from the workload so the same workload reuses it
  (`benchmark_spec` and `verify_benchmark_spec` on `POST /api/campaigns`;
  `POST /api/benchmarks/spec` says what a workload becomes). A replay of a
  rolling dataset pins it for the campaign's life.
- Everything AutoTune creates on LLMBench is filed under the group tag
  `llm-autotune` (`llm-autotune/sweep`, `llm-autotune/replay`); benchmarks it
  created earlier are filed the next time it ensures them.
- Built-in objectives for replay workloads ("Replay: throughput per GPU", and
  the same under a 10s TTFT SLO). The Benchmark step lists only the objectives the
  chosen workload can report.
- The Add machine dialog says, behind its Driver label's `?`, what the
  platform needs before it can reach a machine: the worker's ssh key on the
  box, or a cluster's kubeconfig.

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
- Frontend plugins: a folder in `frontend/src/plugins/installed/<name>/` is
  compiled into the UI and adds pages, nav entries, strings, sections on the
  account and campaign pages (`<PluginSlot>`), and search strategies in New
  campaign. A plugin imports the app only through `@/plugins/api`.
- Reports read as lab reports: a report may name its scenarios
  (`scenario_labels`, migration 003); the summary chart and sweep panels plot
  whole-server throughput per machine; `slo` tables define the SLO and the
  objective; captions are the renderer's own, numbered.
- Reports show the group their runs belong to, linked to its page when it has
  one.
- Resources: a Smoke test button on each machine. GPU count and type stay
  editable on Kubernetes machines, for a credential that cannot read nodes.
- Links in the UI (nav, table rows, names, buttons) are real links, so
  middle-click and Ctrl/Cmd+click open a new tab.

### Plugin API

- Version 1. Hooks: `routers`, `openapi_tags`, `tick_steps`, `migrations`,
  `on_bootstrap`, `propose_candidates` (in-process planning, with a
  `PlanContext` holding the campaign's history with the objective applied),
  `on_campaign_created` and `campaign_extensions` (a plugin's own fields about
  a campaign, as `extensions` on the campaign API, its spec and clones),
  `queue_waiters`, `reservations` and `queue_arrival` (a plugin's own work in
  the machine queue), `submission_extras` (fields for a run's LLMBench
  submission), `run_overlay` and `run_selector` (what the agent API says
  about a plugin's runs, and selectors of its own), and `promotion_origin`
  (how a plugin's campaign winner is named and where it is promoted). Runs a
  plugin keeps in a `RunGroup` compare within the group. The importable
  surface is `app.plugin_api` on the backend and `@/plugins/api` on the
  frontend.

### Changed

- `deploy/quickstart.sh policy` needs `--registry` outside the demo: policy
  pods run on the GPU clusters, which cannot see an image loaded into the
  platform's cluster. The install's closing note and docs/after-installing.md
  say so.
- The campaign page's run control is one group with icons: the way forward
  (Start, Run now, Resume schedule) first, Pause, then Stop. **Force start**
  is now **Run now**, and runs until the search is done or you stop it
  instead of for eight hours (`hours` on `POST /campaigns/{id}/force-start`
  is optional). **Force stop** is **Stop**; on a searching policy the first
  press asks it to wrap up, the second ends it.

- `deploy/quickstart.sh` installs the two platforms only, on a cluster that
  needs no GPUs: it no longer grants itself the cluster it runs in or
  registers it as `local-cluster`, and no longer asks about GPU nodes. GPU
  clusters are added afterwards (above). It sets `publicApiUrl` to the UI's
  address, which serves the API, so policies on a GPU cluster can call back.
- `deploy/quickstart.sh` and `deploy/demo.sh` open both UIs on a NodePort on
  a remote cluster and print `http://<node>:<port>` addresses, so a remote
  install needs no tunnel; each UI's links point at the other's address.
  Port-forward stays the default on a cluster on your own machine, and
  `--port-forward` asks for it anywhere; an ingress set in your values is left
  alone. The chart takes `frontend.service.type` and `.nodePort`.
- The agent API (`/api/agent/v1`) and the Reports pages are off by default:
  they have not been proven on tuning campaigns yet. Set
  `AUTOTUNE_AGENT_API_ENABLED=true`, and build the frontend with
  `VITE_AGENT_REPORTS=1`, to turn them on.
- Quieter pages: explanations moved from paragraphs under fields into `?`
  tooltips, page subtitles folded into the title's tooltip, menu items named
  by their action alone. A workload is entered as labelled fields (input
  tokens, output tokens, concurrency, duration per level; requests and
  concurrency for a replay) instead of a sentence with blanks.

- A bare-metal machine's GPU count and card type are read with `nvidia-smi`
  when it is saved, as a Kubernetes slice's already were from its nodes;
  **Re-read GPUs** works for both. The Add machine dialog asks for a name
  and an address (or a node selector), with the rest under Advanced.
- New campaign: the engine follows the chosen search space instead of being
  asked for, and a campaign left unnamed is named after its model and date.
  The campaign page keeps Report and the run controls in view and moves logs,
  clone and YAML export under **More**.

- A campaign learns how long its runs take — the engine coming up, and each
  benchmark coming back — and plans its window from that (`run_timing` on the
  campaign, migration `004`; shown on the campaign page as "Run length").
  Nothing has to be guessed up front:
  - a policy campaign's validation reserve is learned rather than set;
    `policy_settings.approx_minutes_each` and `model_startup_minutes` still pin
    it when given;
  - `max_run_minutes` is now only a cap, default 720: a new run needs as much
    window as this campaign's runs have taken, and a lease's `returnable_at`
    follows the same figure;
  - before the first run, the benchmark's length is estimated from its own
    settings on LLMBench (a sweep's levels × seconds, a replay's time cap), and
    the engine's startup is bounded by the readiness timeout;
  - a run cut at the window's end counts as a sample of at least the time it
    got, so a too-short estimate corrects itself the next night;
  - with nothing measured or estimated, a run reserves
    `AUTOTUNE_DEFAULT_MAX_RUN_MINUTES`, now 240 (was 150).
- `served_model_name` is optional when creating a campaign: empty means the
  last part of `model_path`.
- New campaign form: the served model name, launch extras, engine port, max
  minutes per run, re-runs of the best and the two-stage benchmark sit under
  an Advanced section of their step; a policy campaign asks only how many
  finalists to re-measure; the merge-request and production-canary settings
  are gone from the form (the API still takes them).

- `deploy/quickstart.sh` now installs a deployment you keep: LLM AutoTune and
  LLMBench on the cluster kubectl points at (`--context` to pick one), with
  runs on its GPU nodes. It warns when no node offers GPUs. The no-GPU
  try-out (kind, the mock engine, the demo campaign) moved to
  `deploy/demo.sh`, and `values-quickstart.yaml` is now `values-demo.yaml`.
  A 0.1.x quickstart install is a demo install: `mv .quickstart .demo`, then
  use `deploy/demo.sh`. `docs/after-the-quickstart.md` is now
  `docs/after-installing.md`.
- The README is shorter: install first, then the demo.
- The README leads with what it does and how it works, with an architecture
  diagram. The docs are rewritten to stop overlapping, every figure is
  redrawn in English and Chinese, and examples, routes and settings match
  the code.
- Scheduling is one machine queue, oldest waiter first: campaigns with a run
  ready and policy sessions waiting for a machine take turns, so two
  campaigns on one machine alternate run by run, and a wide request holds a
  machine it is waiting for instead of being starved by narrow ones.
- Agent API: `LaunchOrigin.source` is an open string with `extensions`,
  `BenchmarkPlatformOut` gains `quality_floors` and `extensions`, run and
  comparison documents carry a `group`, and the LLMBench part says whether
  the benchmark has drifted since the run. A comparison treats `2` and
  `"2"`, `0.9` and `"0.90"`, `true` and `"true"` as the same flag value.
- Pages that refresh poll through one helper that pauses in a hidden tab and
  drops stale responses; nginx serves hashed assets as immutable and never
  caches `index.html`, so a tab open across a redeploy reloads cleanly.
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

### Fixed

- A policy campaign's final validation launched each contender with every
  card the session held, not the ones its config uses: a tp=2 contender on an
  8-card machine asked Kubernetes for 8 GPUs and waited forever when any were
  in use. It now takes tp × dp × pp of them, free ones first.
- **Force stop** on a policy campaign stops it. A policy still starting (its
  pod waiting for a node, say) used to be asked to wind down, which it never
  heard, so the campaign stayed active; it is now aborted and the campaign
  paused. A searching policy is still asked to finish and measure its
  finalists, and a second press aborts it.
- A policy container that can never start (an image that will not pull, a
  crash loop, a pod no node could take) fails at once instead of after the
  30-minute ready timeout; a pod waiting for free cards keeps waiting.
- A policy's live log is read from its own cluster and its own pod; it asked
  the default cluster, for an engine's pod.
- A policy that takes cards gets the same tolerations as an engine, so it
  can land on a tainted GPU node instead of staying Pending; declared
  tolerations (`AUTOTUNE_K8S_TOLERATIONS`) apply to every policy pod.
- Registering a policy no longer gives it the GPUs and the model mount unless
  asked: most policies delegate engine launches, and a registration that took
  both sat waiting for a whole free machine. The Policies list marks the ones
  that take GPUs.
- `deploy/quickstart.sh` and `deploy/demo.sh` say what a pod is stuck on
  while they wait (an image that will not pull, a container crash-looping
  with the end of its log, a pod nothing can schedule) instead of sitting
  silent until the 20-minute timeout.
- A rerun after an interrupted install recovers: a release that never
  finished its first install is removed and installed again, and one stopped
  mid-upgrade is rolled back first.
- The install remembers its cluster's API server as well as the context
  name, and refuses to act when the same name now leads elsewhere (kubeadm
  names every cluster's admin context `kubernetes-admin@kubernetes`).
- Fetching the LLMBench chart retries over HTTP/1.1 when a proxy breaks
  git's HTTP/2, and says how to point at a local chart if GitHub is out of
  reach.
- `deploy/quickstart.sh` no longer prints git's "is not a commit" warning and
  detached-HEAD advice while fetching the LLMBench chart, and a fetch that
  fails part-way is retried on the next run instead of leaving an empty
  directory the script then takes for the chart.
- Runs record the image digest they ran: over ssh the platform asked the
  container for `RepoDigests` and recorded nothing; on Kubernetes it now
  records the pod's `imageID`.
- The overview diagram matches the platform, and the agent API doc's
  examples match its schemas.
- A policy campaign with a nightly window stayed active after its policy
  reported the search exhausted, waiting for a next night that would only
  hear the same. It is now done when its session is.
- The campaign page counted runs over candidates, so a policy campaign, whose
  configs are each a launch and a benchmark, read "18/10 candidates
  evaluated". It now counts candidates.

### Removed

- `deploy/k8s/remote-cluster/` (`backend-rbac.yaml`,
  `make-scoped-kubeconfig.sh`): replaced by `deploy/gpu-cluster.sh`. Its fixed
  names let a second platform's install take over the first's account on a
  shared GPU cluster.
- The GitLab merge-request path: the `gitlab` promotion target and client,
  the deploy-repo file formats, the baselines' bindings to a repo file
  (`/baselines/{formats,branches,import}`, `/baselines/{id}/binding/*`), a
  campaign's deploy branch and auto-promote switch
  (`PUT /campaigns/{id}/deploy-branch`), `/campaigns/{id}/promote` and
  `/promotions`, the `AUTOTUNE_PROMOTION_*` and `AUTOTUNE_GITLAB_*` settings,
  the `promotion_origin` plugin hook, and the frontend plugin exports
  `MergeRequestDialog` and `DeployBranchSelect` (migration `006` drops the
  tables and columns). A winner is a run like any other, with its exact
  launch command and image on its page.

- The CI check that searched the tree for a list of internal host names; that
  list no longer lives in the repository.
- Production capture, clearing and restoring. A lease is now the hand-over:
  the machine is lent to the platform free, campaigns run on it at once, and
  ending the lease stops only the platform's own containers. Gone with it:
  the in-place production benchmark ("baseline canary"), the
  `/machines/{id}/baseline/{capture,clear,restore}` routes, the Override
  menu's Capture/Clear/Restore, `AUTOTUNE_AUTO_BASELINE_LIFECYCLE` and
  `AUTOTUNE_AUTO_RESTORE_PRODUCTION`, `production_status` in the lease API,
  and the machines' `baseline` and `baseline_status` columns (migration
  `005`). Production's config is still measured as a campaign's baseline, by
  launching the config recorded on **Baselines**; the preflight's "matches
  production" check reads that record too. Runs against an endpoint the
  platform did not launch fail as `endpoint_unreachable` /
  `endpoint_unhealthy`.

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

[Unreleased]: https://github.com/modelsphere/llm-autotune/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/modelsphere/llm-autotune/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/modelsphere/llm-autotune/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/modelsphere/llm-autotune/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/modelsphere/llm-autotune/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/modelsphere/llm-autotune/releases/tag/v0.1.0
