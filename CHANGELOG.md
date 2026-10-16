# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Export to model catalog writes the catalog's v2 layout**
  (`catalog.swiss/v2`): one variant file per run, `models/<name>/<id>.yaml`,
  each with its own version, and the model's `metadata.yaml` with a `tuning`
  entry (the gain over the baseline, per workload, and the report). Model
  catalog moved to it and no longer takes new v1 version files.
  - The export reads the published catalog (`catalogIndexUrl`, the catalog's
    `index-v2.json`): a model it already has keeps its name and metadata, a
    variant it already has gets its next version (or no file when nothing
    changed). Unreachable, the model is treated as new, with a warning.
  - Checked against the catalog's current schemas and the rules its validator
    adds; variants ask for the catalog's minimum chart (`sglang >=0.8.0`,
    `vllm >=0.5.0`).
  - **Download** gives one zip laid out as the catalog is
    (`POST /api/catalog-export/archive`), once the export passes.
  - `deploy/dev/check-catalog-export.sh` runs the catalog's own
    `validate:schema` and `validate:helm` on an export.
  - API: the preview returns `files` and `variants`; `version`, `base_entry`
    and `with_metadata` are gone; a variant takes `image_tag`.

### Fixed

- **Pages load faster on a slow connection, and say they are loading.**
  - nginx compresses: the build leaves a `.gz` next to every script and
    stylesheet, and API responses are compressed as they pass through.
  - Element Plus is loaded per component, in the pages that use them, and
    libraries get chunks of their own that stay cached across redeploys.
    The first download goes from 1.5 MB uncompressed to about 180 kB.
  - The page shows a spinner from the first byte instead of a blank page,
    lists show "Loading…" until their rows arrive instead of "No data", and
    a campaign's page shows a spinner, or the error with a retry, instead of
    staying blank.
  - Pages send their requests at once instead of one after another: a
    campaign's page made up to 11 in a row on every 10-second refresh, the
    new-campaign form up to 10.
- Catalog export: a run whose image was named by digest only exported no
  image, so the chart's default engine would have deployed; it now asks for
  the tag. `source.hf` is no longer written from a guess off the weights path:
  a new model needs it given, with the guess offered. A tuned variant without
  its baseline or report is refused instead of published untuned.
- The CI routing file of a track export (`<name>.validation.json`) labels a
  variant without a role `other` rather than `optimized`, and hashes each
  variant file's content instead of its bytes, so the catalog's formatting does
  not break it.

## [0.3.0] - 2026-10-15

### Added

- **Export to model catalog** on a campaign's page (**⋯** menu): measured
  runs become a [model-catalog](https://github.com/modelsphere/model-catalog)
  entry, a `catalog.swiss/v1` version file with one variant per run plus a
  `metadata.yaml` for a model new to the catalog. The baseline and the winner
  are ticked to start; the optimized variant's description carries its gain
  over the baseline. The entry is validated against the catalog's schema, and
  nothing site-specific (host paths, scheduling, placement flags) is
  written. Nothing is pushed: the files are downloaded for a pull request
  ([Reading results](docs/guide/results.md#publishing-it-to-the-model-catalog)).
  API: `POST /api/catalog-export/preview`. Plugins can open the same dialog
  (`CatalogExportDialog` in `@/plugins/api`).
- **Automatic Tuning ▸ Datasets** sets up replay datasets without leaving
  AutoTune: a model, optionally the nodes serving it, a sample size and how
  often to take a new sample. The model and node lists come from the
  gateway's request logs in VictoriaLogs, the form counts the requests that
  match before saving, and everything else has a default under **Advanced**.
  LLMBench collects each sample; the page shows what it holds (window, prompt
  and output lengths, prefix-cache hits) and its recent samples. **New
  dataset…** in a Replay workload's dataset list opens the same form
  ([Datasets](docs/guide/datasets.md)). Needs an LLMBench whose service
  account may create collection profiles.
- New samples are taken by AutoTune on each dataset's interval, and not while
  an unfinished campaign is measuring on the current one (migration
  `008_replay_datasets`).
- `llmbench.victoriaLogsUrl` (`AUTOTUNE_LLMBENCH_VICTORIALOGS_URL`): the
  store the dataset form starts from. `VICTORIALOGS_URL=… deploy/quickstart.sh`
  sets it and turns on LLMBench's dataset collector and volume.
- `deploy/dev/demo-traffic.sh` loads a day of made-up gateway traffic into a
  VictoriaLogs store, to try datasets on a local kind setup.
- **Start from ▸ A previous campaign** on New campaign fills the wizard in from
  a campaign's definition, named with the next version (*sweep v2*), and stays
  on the first step to be changed. **⋯ ▸ Copy to a new campaign** on a
  campaign's page opens the same thing.
- The image, model path and served name fields suggest what was typed before
  in this browser and what the platform's campaigns use.
- The Campaigns list filters by status, owner, model and engine, and searches
  names, models and images. The Runs page filters by status, campaign and
  owner (`GET /runs?status=…&campaign_id=…&owner=…`; `status=live` is every
  live state), and names each run's campaign and owner. The filters are kept
  in the page's address.
- **Logs ▸ Activity** on a campaign (`GET /campaigns/{id}/activity`): what the
  platform did for it, one line per recorded step. It is there even when a
  container printed nothing or never started.
- **Node pools**: a Kubernetes machine whose node selector names a GPU type
  (`nvidia.com/gpu.product=…`) instead of one hostname. Each run goes to
  whichever of its nodes has room, and waits when none has. **Add GPU
  cluster** registers one pool per GPU type by default (or, as before, one
  machine per node; `POST /clusters/{id}/pools`). Campaigns share a pool, and
  a policy session takes as many cards at a time as its widest config needs
  (or `policy_settings.cards`) rather than the whole machine. A pool holds
  one GPU type: leasing a selector that matches two is refused. A policy that
  runs engines in its own container does not use pools
  ([Node pools](docs/guide/machines.md#node-pools)).
- A Kubernetes machine shows its nodes, the most cards one node takes and,
  with `gpuCluster.countFreeCards` (`gpu-cluster.sh --count-free-cards`), the
  cards other workloads leave free. They are read when it is leased or probed
  and every 5 minutes while leased (migration `009_node_pools`). The free count
  needs the platform to list pods in every namespace, which includes other
  teams' pod specs, so it is off by default.
- A run waiting for cards shows **waiting for cards**, and its node once it
  has one.
- **Frozen datasets** in a Replay workload: a frozen LLMBench dataset, a
  file that never changes (frozen there, or imported from another
  installation), next to the rolling samples. Every campaign replaying one
  sees the same requests, with nothing to pin. **Automatic Tuning ▸ Datasets**
  lists them with the campaigns that replay each (`GET /datasets/frozen`); the
  workload spec takes `dataset_frozen`, and a campaign records it
  (`dataset_frozen`, migration `010_frozen_datasets`). Needs an LLMBench whose
  service accounts may list frozen datasets; one that does not simply offers
  none ([Frozen datasets](docs/guide/datasets.md#frozen-datasets)).
- Policy contract 1.2: the manifest's `hardware` has `pool`, `node_count` and
  `node_gpu_max`, and a launch the cluster has no room for reads `queued`
  again, with `waiting_reason` and `waiting_since`. A 1.1 policy keeps working
  ([Evolution](docs/api/policy-contract.md#evolution)).


### Plugin API

- **Version 2.** A plugin written for version 1 must be updated.
- **Removed:** in-process planning (`propose_candidates`, `PlanContext`,
  `RunRecord`) and its frontend half (`strategies` on a frontend plugin). A
  search strategy is a [policy](docs/api/policy-contract.md) container.
  `promotion_origin` already went with the GitLab merge-request path.
- **Managed campaigns:** a plugin runs its work as a campaign the platform
  owns and runs (`create_managed_campaign` from a campaign spec, checked by
  the rules every campaign follows). It changes the campaign only through
  services that refuse a campaign it does not own: `start_managed_campaign`
  (later, optionally on machines it chose), `extend_managed_campaign` (move
  the end of the window), `pin_managed_dataset` (hold several campaigns to
  one dataset build so they compare) and `stop_managed_campaign` (which also
  aborts a live policy session). `record_event` writes to the event log, as
  the plugin or as the person who asked.
  The owner is recorded (migration `011_campaign_owners`): the campaign list
  leaves managed campaigns out unless asked (`GET /api/campaigns?managed=`,
  and a switch on the Campaigns page when plugins are installed), and a
  managed campaign's page links back to the page it runs for
  (`managed_by` on the campaign API).
- **`requires`:** a plugin may build on another. It loads, migrates, mounts
  and ticks after the plugins it requires; a missing or circular requirement
  stops startup.
- **`app.plugin_api` covers what plugins need:** read access to the
  platform's tables, the users, sessions, settings and secrets, machine and
  lease checks, the search space and engine helpers, objectives and result
  summaries, the LLMBench client, dataset pinning, and the agent API's run
  documents. Three rules come with it (docs/plugins.md, "What a plugin may
  touch"): the platform's tables are read-only to a plugin, its own tables
  point at the platform's only by primary key, and it imports another plugin
  only if it requires it.
- **`@/plugins/api` exports the building blocks of the platform's pages:**
  `FactTile`, `ListFilters`, `WorkloadPicker`, `ScoreBars`, `NightlyWindow`,
  `CatalogExportDialog`, and the status, time and filter helpers.
### Changed

- **The quickstart installs LLMBench 0.3.0**, whose service-owned collection
  profiles and frozen-dataset import the **Datasets** page needs.
- **The dataset form's VictoriaLogs address defaults to
  `http://victorialogs.logging:9428`** (chart value
  `llmbench.victoriaLogsUrl`): where the bodylog-victorialogs chart in
  [modelsphere/helm-charts](https://github.com/modelsphere/helm-charts) serves
  it from the namespace its README installs into. The quickstart keeps that
  default unless `VICTORIALOGS_URL` is given; before, it wrote an empty
  address over it.
- The chart's install notes warn when the `runLogs` claim cannot bind: the
  storage class it would get only makes single-node volumes (local-path,
  hostpath, cloud block disks), the cluster has no default class, or the named
  class does not exist. Until now the API and worker stayed Pending without a
  word.
- The campaign page is shorter. The lines of text under the name are now a
  row of tiles (model, search, objective, schedule, progress, replay sample)
  with their details in tooltips; the policy panel is one strip with its
  particulars behind **Details**; the pre-flight result is a row of counts.
  On the leaderboard, the objective and redlines are chips, a column empty on
  every row is left out, and the redline verdict is an icon. The run details
  put the score and the error first and fold the commands and metrics away.
  **Logs** moved out of **⋯** next to **Report**.
- **⋯ ▸ Clone** became **⋯ ▸ Copy to a new campaign**, which opens New
  campaign filled in instead of creating the copy at once. `POST
  /campaigns/{id}/clone` is unchanged.
- New campaign's **Start from a baseline** and **Import YAML** buttons are one
  **Start from** menu.
- A future time reads "in 5 h" rather than as a date, and a window's next
  start is no longer shown twice.
- An empty run or policy log says why it is empty (stopped before its engine
  launched, no container of its own, or printed nothing) instead of
  "(no captured log)".
- The cluster is asked why a pod is not running from 30 seconds after launch,
  not only after the 45-minute image pull budget: a run waiting for cards
  says so at once, and one no node could ever hold (more cards than the
  largest node has) fails at once instead of 45 minutes later. A policy launch
  wider than the pool's largest node is refused when requested.
- A run that waited for cards starts its clocks when it gets them: the image
  pull and model load budgets, and the startup time it teaches the
  campaign's window planning, no longer count the time it was queued. Before,
  a run placed after 45 minutes in the queue failed its pull budget at once.
- A finalist whose validation run never got cards before the night ended is
  *skipped* ("the cluster had no free cards for it"), not *failed*.

### Fixed

- Policy containers start with `PYTHONUNBUFFERED=1`, so what a Python policy
  prints reaches its log; buffered output was lost when the container was
  removed. The random-search policy's logs were also empty because its SDK
  logged nothing; the SDK in llm-autotune-policies now logs each step.
- A pod that printed nothing no longer leaves its name as the whole saved log.
- A policy log that cannot be written to the worker's disk no longer stops the
  policy container's teardown.
- A run held up for a policy (*serving*) counts as live in the UI: it has a
  **Stop** button and is not counted as finished.
- Engine and policy logs were read only once, when the container ended; a pod
  already gone by then (evicted, its node lost, the worker restarting) left
  no log. The worker now saves the end of every running container's log every
  two minutes, and an empty final read keeps that copy. A policy that dies
  after its pod is gone shows the last saved lines as its death log.
- Saved logs are bounded: the last 2,000 lines (up from 400 for engines and
  200 for a dead policy), at most 1 MB per file (`AUTOTUNE_LOG_SNAPSHOT_SECONDS`,
  `AUTOTUNE_LOG_LINES`, `AUTOTUNE_LOG_FILE_MAX_BYTES`).
- A running engine's log is read from its container first, not from the copy
  saved minutes ago.
- An empty log says why: a run whose pod the cluster never placed shows the
  scheduler's reason (for example, not enough GPUs), and a policy log tells a
  container that never reached the platform from one that was gone before it
  could be read.

- `helm upgrade` migrates the schema before it replaces any pod: a
  `pre-upgrade` Job runs `app.db.bootstrap --schema-only`. The migration ran
  after the new pods started, and for that half a minute the new API answered
  500 against the old schema, which ended a policy session running through the
  upgrade. Migrations must stay additive for this; CONTRIBUTING says so.
- The bundled Postgres no longer restarts on every chart version: its pod
  carried the chart's version label. With `postgresql.storage.enabled=false`
  that restart emptied the database on every upgrade. The mock LLMBench and
  the mock model's pods dropped the version label too.
- The API and frontend pods keep serving for 8 seconds after they are told to
  stop (`preStop`), so a rolling upgrade drops no request.
- The API and worker restart when the Secret changes (a new LLMBench key or
  admin password), as they already did for the ConfigMap.

## [0.2.2] - 2026-10-12

### Added

- A user guide, [docs/guide](docs/guide/README.md): the concepts, a
  first campaign from a fresh install with a screenshot of every step, and a
  page for each part of the UI (machines, search spaces, objectives, policies,
  creating, running and reading campaigns, baselines, runs and failures,
  accounts and API keys), plus troubleshooting.
- The end of every captured engine and policy log is kept in the database
  (migration `007_log_tails`), so a run's log shows in the UI on an install
  without a shared log volume; a running engine's log is read live.
- New campaign's **Schedule** step asks **Every night** or **No window (start it
  by hand)**, instead of needing both times cleared.
- `allowSignup` (`AUTOTUNE_ALLOW_SIGNUP`, default `true`) turns off **Create
  account** on the sign-in page, and admins can **Add user** on the Account
  page.
- Registering a GPU cluster's nodes leases them in the same step (**Lease them
  to the platform now**, on by default).
- A run's details show its **Engine command**, the engine's command line for
  that config with a copy button, next to its image and weights path; on
  Kubernetes the launch command is the whole applied manifest. Leaderboard
  rows open their run, and a row on the **Runs** page opens the run's details
  instead of only its campaign.
- **⋯ ▸ Mark as done** closes a paused or scheduled campaign, which otherwise
  held its machines (they could not be edited or removed) with no way to end
  it. **⋯ ▸ Delete** removes a campaign that never ran.

### Changed

- Kubernetes pre-flight checks use two probe pods per machine, one pulling the
  images and one mounting the model path. A wrong image and a wrong path are
  reported in the same run; before, a failed mount left the images unchecked.
- **Add machine** reads the GPU count from the machine and refuses to save
  when it cannot be read and none was given, instead of assuming 8 GPUs.
- A policy campaign's **Runs** tab shows one row per trial, with the benchmark
  run nested under the engine it measured.
- A campaign drafted from a baseline uses the matching built-in objective
  (**Throughput per GPU**) instead of an inline copy named "drafted: <metric>".
- The UI spells the product name "LLM AutoTune"; the schedule dialog says what
  was saved ("No window: the campaign runs when you start it").
- Docs: the Chinese pages and diagrams are removed (the UI keeps its Chinese
  translation), and the English docs use consistent, technical wording.

### Security

- API keys: a user lists and revokes only their own keys; an admin sees every
  user's, with its owner. Keys the platform mints for a policy session are no
  longer listed or revocable from the page, where revoking one cut a running
  search off.

### Fixed

- A policy's finalists were measured on the platform's default benchmark and
  ranked by the replay metric when the campaign had no verify stage. They are
  measured on the campaign's own benchmark and judged by its own objective, on
  a board titled "The policy's finalists, measured again".
- **Stop**, then **Start**, finished a campaign with the configs Stop cut off
  never measured. A campaign Stop queues them again.
- The campaign report said no baseline was measured when a recorded baseline
  had been relaunched in the campaign; it now compares against it.
- On Kubernetes, a crash-looping engine's log was read from the restarted,
  empty container, so an out-of-memory crash was filed as `crash_loop`. The
  log from before the restart is read too, and every captured log is decoded
  text instead of a Python bytes literal.
- **Add machine** with an empty form created a machine with no name or host;
  a name, and a host for an ssh machine, are required.
- Error messages from a refused form (a list of field errors) showed as an
  empty red toast on every page.
- Search spaces: Enter now adds the parameter or value typed (the first value
  typed into an empty list was glued to the next one), a range previews
  without float noise (`0.8500000000000001`), its inputs are wide enough to
  read, and a parameter left without a value is named on save instead of
  dropped.
- A new objective started with a functional-pass-rate redline that the
  synthetic sweep never reports, so every config was rejected. It starts with
  none; **Add redline** suggests p99 TTFT with an empty limit, shows the
  metric's unit, and the **Auto** direction shows as selected.
- New campaign: the **Check** step showed a green tick before any check ran;
  **Create campaign** went ahead with known blocking problems without asking;
  an imported or drafted search space showed 0 candidates; importing a file
  with `daily_start: null` failed; the Strategy and replay Dataset selects
  showed "Select" for Grid and the example set; the default name used the UTC
  date.
- Campaign page: the schedule line of a finished campaign read "Awake now";
  Start read "Force-started — running until" a date a month away; a failed
  run's error sat below its long launch command (now first, with the command
  folded); stopped runs offered "Re-run failed"; the policy panel showed its
  end reason as an identifier.
- Wording: run cards show as `#0 #1` (a bare `0` read as no GPUs), stopped runs
  as "stopped", the platform's own withdrawals as "queued again", the policy
  runs' role (engine, benchmark, finalist), the baseline on the Candidates tab,
  the policy's name in the campaign list, the parameter names over the
  leaderboard bars, and plain words instead of "canary" in the report.
- Users who are not admins no longer see cluster actions the API refuses them;
  the Account page's note on what admins can do was wrong.
- The API key dialog's example call used a fixed sample address instead of the
  platform's.
- Removing a machine that a policy session had used failed with a server
  error; removing a machine in a node group now says to take it out of the
  group first.
- Smaller fixes: the Baselines table no longer overflows the page, the
  Policies page header lays out like the others, an unleased machine no longer
  shows a fleet-manager readiness tag, and the smoke test's card-type warning
  names the menu item that exists.

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
  ([installing](docs/install.md#local-setup-with-kind)).

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

[Unreleased]: https://github.com/modelsphere/llm-autotune/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/modelsphere/llm-autotune/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/modelsphere/llm-autotune/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/modelsphere/llm-autotune/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/modelsphere/llm-autotune/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/modelsphere/llm-autotune/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/modelsphere/llm-autotune/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/modelsphere/llm-autotune/releases/tag/v0.1.0
