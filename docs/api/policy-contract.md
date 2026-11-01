# Policy Session API

How an external **policy** — a container image holding a search algorithm —
runs a night of tuning on a machine LLM AutoTune manages, and how the platform
delivers its verdict.

This document is the narrative version: what the calls mean, and what each side
guarantees. For the one-page summary see [policy-api.md](policy-api.md); the interactive reference that
always matches the running build is **`/api/docs`**, section *policy sessions*.
For real responses from a finished session see the
Contract version: **1.7** (`/api/policy/v1`; see [Evolution](#evolution)).

---

## Summary

The platform starts your container on a GPU machine with a time budget and an
API token. You dial out: fetch the manifest, heartbeat every 30 seconds, and
spend the night however you like — ask the platform to launch engines for you,
or serve them yourself and ask the platform to benchmark them; run your own
tests; ignore every service and bring your own loop. Two obligations are not
optional: **say what you are tuning** (`PUT /session/plan`) and **keep your
best-so-far list current** (`PUT /contenders`). When the search deadline
arrives the heartbeat tells you to stop. The **platform then validates your
best contender itself** — it launches the config from the spec you registered
and measures it with the full replay, the same way for every campaign, so the
verdict never depends on how you would have served it. (The one exception: a
contender the platform cannot launch — a spec on a *different* engine image
because you serve your own engines — is one it asks *you* to serve.) That
measurement — never your own — is the campaign's verdict. Production is
measured the same way, at the same time, so the verdict says whether a
contender beats it under the same conditions; validation launches run side by
side, as many as the night's GPU budget holds. Then you exit, the
container is removed, and production is restored exactly as it is after any
other night.

---

## Connection direction

**You dial out. The platform never dials into your container.**

- Control reaches you only as the response to your own heartbeat.
- Platform services (launch, benchmark, state, …) are HTTP calls you initiate.
- The only inbound connections to the machine are the ones every engine
  already receives: the platform's health probe and the benchmark platform
  (LLMBench) driving load against an engine port.

Consequences worth knowing: the platform's worker acts on your requests on its
next tick (~10 s), so every round-trip has that latency floor — plan in
minutes, not milliseconds. A platform restart is invisible to you: keep
heartbeating, requests retry safely (see [Idempotency](#conventions)).

---

## Boot

Your container starts with three environment variables:

```
AUTOTUNE_API_URL      e.g. http://192.0.2.10:28100
AUTOTUNE_API_KEY      atk_…   (session-scoped; dies with the session)
AUTOTUNE_SESSION_ID   17
```

Any variables registered with the policy come too: its plain `env`, and its
`secret_env` (1.3) for credentials such as an agent policy's LLM API key,
which the platform stores encrypted, never returns, and redacts from the
container's recorded launch command.

Send the key on every request as `X-API-Key`. It is valid for **this session
only**: it cannot touch other campaigns, other machines, or other sessions,
and it expires shortly after the session ends. Everything you do is audited as
`policy:<name>`.

First calls, in order:

1. `GET /api/policy/v1/session` — the manifest (below).
2. `POST /api/policy/v1/session/heartbeat` — with your capabilities (below).
3. `PUT /api/policy/v1/session/plan` — what you intend to tune.

Then search.

---

## The manifest

`GET /session` returns everything the night is made of. Re-read it whenever
you like — **deadlines can move** (an operator force-stop shortens them), and
the response is always current.

```json
{
  "contract": {"version": "1.0"},
  "session_id": 17,
  "campaign_id": 42,
  "policy": {"name": "my-policy", "image": "registry/my-policy:1.3.0", "version": "1.3.0",
             "params": {"model": "my-llm"}},

  "model": {
    "engine": "sglang",
    "image": "lmsysorg/sglang:v0.5.3",
    "model_path": "/model",
    "served_model_name": "qwen3-32b",
    "extra_env": {},
    "extra_volumes": {}
  },

  "hardware": {
    "machine": "gpu-h100-1",
    "gpu_type": "H100",
    "gpu_indices": [0, 1, 2, 3],
    "ports": [28200, 28201, 28202, 28203],
    "gpus_visible_in_container": true,
    "pool": false,
    "node_count": 1,
    "node_gpu_max": 8
  },

  "objective": {
    "screen":  {"target_metric": "perf_guidellm_sweep.output_tpm_card_norm", "direction": "max", "redlines": []},
    "verify":  {"target_metric": "replay.output_tpm_card_norm", "direction": "max", "redlines": []}
  },

  "search_space": {"base": {}, "grid": {}, "tied": [], "range": {}, "conditions": {}},
  "space_policy": {"deviations": "allowed_if_declared",
                   "forbidden": ["quantization*", "kv_cache_dtype", "context_length", "…"]},

  "production": {"engine_args": {"tp": 8, "mem_fraction_static": 0.83, "…": "…"},
                 "env": {"PYTORCH_ALLOC_CONF": "expandable_segments:True"},
                 "image": "lmsysorg/sglang:v0.5.22",
                 "config": {}, "source": "recipe", "launch_command": ""},

  "time": {
    "started_at": "2026-08-20T15:02:11Z",
    "search_deadline": "2026-08-21T05:45:00Z",
    "hard_deadline": "2026-08-21T08:00:00Z",
    "heartbeat_interval_s": 30,
    "heartbeat_timeout_s": 180,
    "tick_s": 10
  },

  "contenders": {"max": 2, "validation_suite": "verify", "approx_minutes_each": 60},

  "services": {
    "launch": true,
    "benchmarks": {
      "screen": {"slug": "autotune-screen-v1", "approx_minutes": 10, "dataset_build_id": "b-20260818",
                 "workload": [{"scenario": "replay", "module": "replay", "family": "replay",
                               "params": {"concurrency": 16, "max_samples": 400},
                               "reads_as": "Recorded production requests, replayed at one fixed concurrency: …"}]}
    },
    "state": true,
    "diagnostics": true,
    "history": true
  },

  "prior": {"contenders": []},

  "measures": [{"name": "output_tpm_per_gpu", "label": "Output tokens/min per GPU", "unit": "tokens/min",
                "better": "higher", "group": "throughput", "meaning": "…", "stats": []}, "…"]
}
```

Field notes:

- **`hardware`** — the cards and host ports that are yours tonight: the
  night's **GPU budget** (the campaign's `policy_settings.cards`). Every launch
  you make shares it, each taking as many cards as its config uses, so the
  budget — not a count of launches — is what limits how much runs at once. Use
  no others. Engines you serve yourself must bind `0.0.0.0:<one of these ports>`
  (the container runs on the host network). If `gpus_visible_in_container` is
  true the cards are already visible to your process.
  With `pool: true` (1.2) the machine is a Kubernetes node pool, and
  `gpu_indices` are **slots**, not devices: how many cards you may use at
  once. Each launch's pod goes to whichever of the pool's `node_count` nodes
  has room, with all its cards on one node, so a launch needs no more than
  `node_gpu_max` cards (0 = not known). Naming slots works exactly as naming
  cards does.
- **`search_space`** — the campaign author's declared space, in the platform's
  vocabulary (see [Config format](#config-format)). `space_policy` says how strictly it
  binds you: `allowed_if_declared` means you may step outside it — an unknown
  flag, a value off the grid — provided your `plan` says so.
- **`policy.params`** (1.3) — the campaign's knobs for *your* policy (an
  agent's model name, its effort, a budget), set in the campaign's policy
  settings and handed over verbatim. The platform never reads them; your
  README says what you accept.
- **`space_policy.forbidden`** (1.3) — parameters you may not change (see
  [Config format](#config-format)).
- **`objective`** — per suite: the metric the platform will reduce results to,
  its direction, and the redlines a config must hold. Your trials are scored
  with exactly this, so you never need to re-implement it.
- **`production`** — what serves today, as the platform launches it when it
  measures production next to the contenders (1.7). `engine_args`, `env` and
  `image` are what it runs, read from the recipe the platform builds: every
  flag, not a diff. `config` is what to send to launch exactly that — the keys
  over the campaign's anchor, so `{}` when production is the recipe as written.
  `source` says where it came from: `baselines` (a recorded reference, applied
  over the recipe), `recipe` (none recorded, so the campaign's recipe as
  written) or `campaign` (a campaign without a recipe: its fields and base).
  Measure `config` first as your anchor; it is the same launch the platform
  validates as production.
- **`time`** — `search_deadline` is when exploration must stop (validation
  time is already reserved after it); `hard_deadline` is the end of the
  window, when anything still running is killed.
- **`contenders`** — how many contenders the platform will validate, and
  about how long each validation benchmark takes. The platform learns that
  figure from the campaign's own runs, so it is a fallback on the first night
  and a measurement after that; the reserve is `max` × (benchmark + model
  startup) plus a margin.
- **`services`** — tonight's menu. Absent entry = unavailable tonight. Never
  assume a service; read the menu.
- **`prior`** — validated contenders and verdicts from this campaign's earlier
  nights, so you can warm-start.
- **`services.benchmarks.<suite>.workload`** (1.3) — what the suite sends, one
  entry per module in benchmark order: its family, the params that shape the
  numbers (never a credential), and in `reads_as` what its measures describe —
  for example, that a replay has no concurrency levels. Empty when the
  benchmark platform could not be asked.
- **`measures`** (1.3) — the [measure catalogue](measures.md): every
  measure's unit, direction (`better`), meaning and statistics. A benchmark's
  `measures` (below) use these names, so a policy can render and compare any
  module's results without knowing the module.

---

## Heartbeat

```http
POST /api/policy/v1/session/heartbeat
{"phase": "searching", "message": "trial 14/40, best 1.06x", "progress": 0.35}
```

First heartbeat only, add your handshake:

```json
{"phase": "starting", "sdk_version": "0.3.1", "contract_version": "1.1",
 "capabilities": ["serve"]}
```

`phase`, `message` and `progress` are free-text/number for the humans watching.
Two optional fields (v1.1) are **read by the machine**:

```json
{"phase": "searching", "status": "exhausted",
 "coverage": {"fraction": 1.0,
              "dimensions": [{"param": "tp_size", "tried_values": [1, 2, 4]}]}}
```

- `status` — an explicit lifecycle signal: `"exhausted"` (you have nothing left
  to try — finalize me now) or `"error"` (your search logic broke and you are
  giving up; add a `failure_class`). Omit it while still working. The platform
  acts on it immediately — a `finalize` comes back on the very same beat —
  instead of making you idle to the deadline. It never *depends* on it (see the
  watchdog below), so a policy that forgets it still winds down correctly; but
  sending it is the cooperative thing to do.
- `coverage` — how far into the declared space you have searched, in the
  platform's own vocabulary (parameter names from `search_space`). This is the
  *supplement* to what the platform derives itself from your trial ledger: a
  completion `fraction` for an infinite/model-based space, and per-axis
  `tried_values` / `tried_ranges` / `skipped_values` (values you are
  deliberately not trying). The platform's own derived coverage is
  authoritative; yours is badged self-reported.

Response, always:

```json
{"command": "run",
 "search_deadline": "2026-08-21T05:45:00Z",
 "hard_deadline": "2026-08-21T08:00:00Z"}
```

| `command` | You must |
| --- | --- |
| `run` | Carry on. |
| `finalize` | Stop exploring, tear down your engines, free cards and ports, then `POST /session/finalized`. |
| `serve` (+ `contender_id`, `port`) | **Only for a contender the platform cannot launch itself** (a spec on a different engine image): serve exactly that contender on that port, then `POST /contenders/{id}/serving`. Contenders on the campaign's own image are launched and measured by the platform, not served — you will not be asked to serve those. |
| `exit` | Tear everything down and exit 0. |
| `abort` | Exit immediately; nothing more will be measured. |

Rules: heartbeat at least every `heartbeat_interval_s`; act on a command
within ~60 s; treat an unrecognized command as `run` and say so in your next
heartbeat `message` (new commands are only ever sent to policies whose
handshake declared the matching capability, so this is a belt-and-braces
rule). If you fall silent for `heartbeat_timeout_s` while your container is
alive, the platform assumes you are wedged; at twice that, the session fails —
your registered contenders are validated anyway, since the platform launches
and measures them itself regardless (the default path for every campaign).

**Liveness is not productivity.** A heartbeat proves you are alive, not that
you are getting anything done — a policy can beat forever while exhausted or
livelocked. So the platform *also* watches the delegated work it can see: if a
searching session has nothing running and makes no launch or benchmark request
for `search_idle_timeout_s`, it earns an idle strike, and
`search_idle_strikes` consecutive strikes (default 3 × 5 min) end the search
and finalize you — whether or not you ever sent `status: "exhausted"`. Any
launch, benchmark, or run completion resets the count, so real work — including
a long benchmark still in flight — never trips it. This is the enforceable
backstop; the `status` signal is just the fast, polite version of the same
outcome. Why a session left `searching` — `policy_exhausted`, `policy_error`,
`policy_idle`, `deadline`, `policy_wedged`, `container_died` — is recorded and
shown in the UI.

Miss neither direction of the clock: the platform owns it. A force-stop from
the UI simply moves `search_deadline` to *now* — your next heartbeat returns
`finalize` and the night proceeds to validation as usual.

---

## The plan

```http
PUT /api/policy/v1/session/plan
{
  "tuning": ["chunked_prefill_size", "max_running_requests", "tp_size"],
  "extras": [
    {"param": "enable_torch_compile", "values": [true, false],
     "reason": "not in declared space; strong prior from sglang release notes"}
  ],
  "notes": "Bayesian search, warm-started from 2 prior nights"
}
```

`tuning` — which of the declared space's parameters you will actually vary.
`extras` — parameters you intend to introduce that the space does not declare,
with a reason. Re-PUT whenever your intent changes.

This is the cooperative-trust core of the contract. The platform will not
reject an out-of-space config — it records a **deviation badge** on every
trial and contender that carries one, visible in the UI, on the leaderboard
and in the report. Declared extras are expected deviations; undeclared ones
are still accepted but flagged louder. The platform never silently rewrites
your configs; the only hard rejections are safety ones (a config that cannot
fit the machine's cards, a malformed value — the same `validate_config` every
platform's own expansion faces).

<a name="configs"></a>
## Config format

Every config anywhere in this API is a **flat JSON dict of engine args** in
the platform's canonical spelling: snake_case keys (`tp_size`, not
`--tp-size`), JSON-typed values (real booleans). **Placement is never yours**:
`model_path`, `host`, `port`, `nccl_port`, `dist_init_addr`, `node_rank`,
`nnodes` are added by whoever launches — omit them everywhere.

The platform canonicalizes what you send (merges the space's `base`, prunes
parameters inactive under `conditions`) and **echoes the canonical config
back**. The echo is the truth: it is what gets hashed for dedup and history,
and the SDK computes the same form locally so you can dedup before spending a
launch.

Two more rules (1.3) make every measured config one that can be deployed:

- **Forbidden parameters.** `space_policy.forbidden` lists parameters (fnmatch
  globs over canonical keys) you may not change. The benchmarks measure speed,
  not answers, so the default list is the flags that change what the
  deployment returns or can do: KV-cache and weight precision, context length,
  chat template, tool-call and reasoning parsers, default sampling, tokenizer.
  You may keep the value the space's `base` gives such a parameter, and a
  parameter the campaign's own space sweeps is never forbidden. A campaign can
  replace the list (`policy_settings.forbidden_params`; empty = nothing
  forbidden).
- **The command-line round trip.** A config must come back unchanged after
  being rendered to the engine's command line and parsed back — exactly what
  every launch and every captured deployment goes through. So values are
  scalars: a string, a number or a boolean. A list or an object, an empty
  string, or a value starting with `--` cannot make the trip; give a JSON flag
  its value as one JSON string.

Both are refusals (`422`) at `POST /launches`, `POST /benchmarks` and
`PUT /contenders` (where the entry is echoed in `rejected`), and both are
reported without refusing by `POST /configs/check` below.

**Environment variables (1.5).** Some engine switches exist only as
environment variables (`SGLANG_*`, `VLLM_*`). A config sets one as an
`env.<NAME>` key, still a flat scalar: `{"tp_size": 2,
"env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM": true}`. The platform keeps these keys off the
command line and puts them into the engine container's environment, over the
campaign's `extra_env`; a boolean becomes `1`/`0`, anything else its string. A
search space can sweep them like any parameter, and they travel with a winner
into its deployable config. Refused (`422`, and reported by
`POST /configs/check` as `unknown_env`, `placement` or `bad_key`):

- a name the campaign's image does not read, per its
  [vocabulary](#engine-vocabulary-15). The engine would ignore it silently, and
  the trial would measure the config without it. The message names the
  nearest names it does read. With no vocabulary for the image yet, every env
  key is refused, unless the campaign sets
  `policy_settings.env_without_vocabulary: "warn"`;
- placement and credentials: device visibility (`CUDA_VISIBLE_DEVICES`, …),
  rendezvous (`MASTER_ADDR`, `RANK`, …), fabric (`NCCL_SOCKET_IFNAME`, …),
  any `*_PORT` or `*_HOST_IP`, search paths, and tokens, keys and passwords;
- a name that is not `A-Z`, `0-9` and `_`.

**Recipes (1.6).** Every launch is a *recipe*: the engine chart's values
(`{engine, chart, values}`), exactly as a [model-catalog](https://github.com/modelsphere/model-catalog)
variant writes them. The manifest's `model.recipe` is the one every launch
starts from, the space's `base` applied, and a config's keys are addresses
into it:

| key | address |
| --- | --- |
| `tp_size` | a flag in `values.extraArgs`, as before |
| `env.SGLANG_X` | an entry of `values.env`, as before |
| `prefill.mem_fraction_static`, `decode.max_running_requests` | a flag in a pd role's `values.pd.<role>.extraArgs` |
| `values.pd.prefill.replicas`, `values.localKVOffload.hicache.ratio` | any chart value, by its dotted path (`values.resources.limits["rdma/hca_shared"]` for a segment with dots) |

`null` removes: a flag, a variable, a value. A config without the new prefixes
means what it always meant. Refused as `bad_key`: an address that does not
parse, and `values.extraArgs` / `values.env` as a whole (set their entries by
their own addresses). Refused as `placement`: what only the site or a run sets
(scheduling, routing, scaling, naming, the Service type, the image). Refused as `substrate`: a pd role flag or chart value
on a machine whose launches are not LLMServices, since its driver applies
flags and env only.

A policy sends configs only; the platform builds the recipe. `POST
/configs/check` answers with the `recipe` a launch of the config runs, its
`shape` (the pods and the cards each claims) and `commands` (each role's engine
command as the chart builds it), so a policy can see exactly what would run
without reading the chart. A recipe campaign also refuses what its chart would
(`not_renderable`, `placement`): a flag the chart renders itself (`--port`,
`--model-path`, …), an image without a tag. A winner is published as the
recipe its config builds.

---

## Services

All optional. What is on tonight's menu is what the manifest's `services`
object lists.

### Delegated launch

```http
POST /api/policy/v1/launches
Idempotency-Key: t14-launch
{"engine_args": {"tp_size": 2, "chunked_prefill_size": 4096}, "gpu_indices": [0, 1], "port": 28201}
```

The platform validates (safety only), launches the campaign's engine image
with your args on your cards, health-gates it, and holds it **serving** until
you release it. `gpu_indices` names as many of the manifest's cards as the
config uses (tp×dp×pp); on a machine with no cards (`hardware.gpu_indices`
empty, e.g. a CPU-only test cluster running a mock engine) it is `[]`. Poll:

```http
GET /api/policy/v1/launches/31
→ {"status": "serving", "endpoint_url": "http://198.51.100.51:28201",
   "launch_command": "docker run …", "failure_class": "", "error": ""}
```

`status`: `queued → launching → ready → serving`, or `failed` (with the same
`failure_class` taxonomy campaigns use: `oom`, `bad_config`, engine crash —
an OOM is information, learn from it), then `released` after the release
below.

On a shared cluster a launch can go back from `launching` to `queued` (1.2):
the cluster has no room for its pod yet. `waiting_reason` says what the
cluster said, and `waiting_since` since when. It starts on its own once cards
free up, and never fails for waiting; keep polling, or release it to try
something smaller. A config no node could ever hold is refused when launched
(`422`), or fails at once with `failure_class: "unplaceable"`.

Release:

```http
DELETE /api/policy/v1/launches/31
```

Concurrent launches on disjoint card sets are allowed. Expect launch → serving
to take minutes (model load), never seconds.

### Benchmarks

Two forms, one for each way an engine can exist:

```http
POST /api/policy/v1/launches/31/benchmarks        # engine the platform launched
{"suite": "screen"}

POST /api/policy/v1/benchmarks                    # engine you serve yourself
{"port": 28202, "engine_args": {…}, "gpu_indices": [2, 3], "suite": "screen"}
```

For a self-served engine you must say what is behind the port (`engine_args`,
`gpu_indices`) — that is what makes the measurement a *trial* rather than a
number. The platform health-checks the endpoint, submits it to LLMBench, and
reduces the metrics with the campaign's objective:

```http
GET /api/policy/v1/benchmarks/57
→ {"status": "succeeded",
   "summary": {"objective_value": 1234.5, "feasible": true,
               "constraints": [-120.0], "breaches": []},
   "metrics": {"perf_guidellm_sweep.output_tpm_card_norm": 1234.5, "…": "…"},
   "measures": [{"scenario": "perf_guidellm_sweep", "module": "perf_guidellm_sweep", "kind": "sweep",
                 "operating_level": "c16",
                 "operating_rule": "the level with the highest total throughput that met the SLO",
                 "operating": {"output_tpm_per_gpu": 154.3, "ttft_ms@p99": 812.0, "success_rate": 1.0, "…": 0},
                 "points": {"peak": {"…": 0}, "c1": {"…": 0}, "c16": {"…": 0}},
                 "supports": ["itl_ms@p50", "…"]}]}
```

`measures` (1.3) is the same result in [measures](measures.md), one entry per
module. Read it in preference to `metrics`: a measure means the same thing
whichever module reported it, while a raw key does not (a replay's
`output_tps_mean` is one request's speed; a sweep's is the whole level's).
`operating` is the scenario's operating point: for a sweep, the level with the
highest total throughput that met its SLO (`operating_level`); for anything
else, the whole run (`operating_level` is null). A key absent from `operating`
was not reported — never read it as 0. `supports` lists every key the module
can report at all.

`suite` names an entry from `services.benchmarks`. Tonight's menu tells you
each suite's approximate duration and pinned dataset. (v1 offers `screen`;
the full replay `verify` suite is reserved for the platform's own validation.)
A benchmark whose `approx_minutes` no longer fits before `search_deadline` is
refused with `409` — do not start what cannot finish.

### Trials

```http
POST /api/policy/v1/trials
Idempotency-Key: t14
{"config": {…}, "source": "self",
 "reported_objective_value": 1180.2, "reported_feasible": true,
 "failure_class": "", "duration_seconds": 312, "notes": "own 30s smoke bench"}
```

Platform-measured benchmarks become trials automatically (`source:
"platform"`). Self-measured ones exist only if you report them. Report
everything: trials are the observability stream the UI shows live, and the
cross-night history your future self warm-starts from. Self-reported numbers
are displayed with a *self* provenance badge and never rank anything.

### Contenders

```http
PUT /api/policy/v1/contenders
{"contenders": [
  {"rank": 1,
   "launch_spec": {
     "engine_args": {…complete canonical config…},
     "image": "lmsysorg/sglang:v0.5.3",
     "env": {}, "volumes": {},
     "environment": {"engine_version": "0.5.3", "torch": "2.8.0",
                     "cuda": "12.8", "image_digest": "sha256:…"}
   },
   "evidence": {"screen_objective": 1234.5},
   "trial_ids": [12, 31]},
  {"rank": 2, "launch_spec": {…}, "evidence": {…}, "trial_ids": [40]}
]}
```

Replace-the-list semantics; up to `contenders.max` entries. **Update it every
time your best changes** — if you crash at 04:00, what is registered is what
gets validated. A `launch_spec` must be complete enough that the platform can
launch it without you — because it does, by default: the platform launches
every contender itself to take the verdict (whether or not you are still
alive), and only asks you to serve one it *cannot* launch (a different engine
image). `environment` is what makes a verdict *promotable* — fill it. The list
freezes at finalize (`409` after).

### State

```http
PUT /api/policy/v1/state          # opaque blob, ≤16 MB, any content-type
GET /api/policy/v1/state
```

Per campaign, not per session: night 2 reads what night 1 wrote. The platform
never looks inside.

### History (1.4)

When the manifest's `services.history` is true. State stays within one
campaign; history is what other campaigns learned.

```http
PUT /api/policy/v1/session/digest
{"summary": "Prefill CUDA graphs were the one large lever …",
 "lessons": [{"text": "chunked_prefill_size above 16384 cannot launch at the default memory fraction", "scope": "model"}],
 "constraints": ["enable_mixed_chunk wedges the scheduler under the replay"],
 "open_questions": ["does lpm compose with breakable prefill graphs?"]}
→ 204
```

Write the digest once your search is over (replacing an earlier one is
fine). It is the night's findings in your own words, the way you would brief
whoever tunes this model next. A lesson's `scope` says how far it reaches:
`campaign` (this workload and objective), `model`, `engine` or `hardware`.
Accepted until the session ends. Limits: a 20,000-character summary, 50
lessons, 50 constraints and 20 open questions.

```http
GET /api/policy/v1/history?match=model&limit=10
→ [{"campaign_id": 35, "campaign_name": "…", "session_id": 18,
    "ended_at": "…", "end_reason": "explicit",
    "conditions": {"engine": "sglang", "image": "…", "model_path": "…",
                   "served_model_name": "…", "gpu_type": "H800", "cards": 8,
                   "benchmark_slug": "…",
                   "objective": {"target_metric": "output_tpm_per_gpu", "direction": "max", "redlines": […]},
                   "forbidden": […]},
    "verdicts": [{"config": {…}, "is_production": true, "value": 44774.2, "vs_production": null, "holds_redlines": true},
                 {"config": {…}, "is_production": false, "value": 45933.8, "vs_production": 1.026, "holds_redlines": true}],
    "digest": {…}}]
```

Other campaigns' nights, newest first, from campaigns of the same owner and
the same engine. With `match=model` (the default) the model must match too.
A night appears once it has a digest or a validation.

- **`conditions`** are the platform's own record of what the night ran under,
  not the policy's account of it.
- **`verdicts`** are the platform's validation at the end of that night:
  production and each finalist, measured again, with `vs_production` as a
  multiple of production's value on the same night. A finalist with
  `same_as_production` runs production's own config, so its `vs_production`
  is the validation's noise, not an effect. On a short benchmark that noise
  has been seen at several percent.

Read another campaign's findings as hints, not facts. A different image,
workload or objective can turn them over, so check them with this night's
trials before relying on them.

### Engine vocabulary (1.5)

When the manifest's `services.vocabulary` is true. The flags and environment
variables the campaign's engine image reads, read from that image (one dump
per image digest), each with the engine's own description where the image or
its docs have one, and the platform's recommendation:

```http
GET /api/policy/v1/engine/vocabulary?kind=env&level=mid&query=spec
→ {"status": "ok", "engine": "sglang", "engine_version": "0.5.19", "total": 1,
   "notes": [], "entries": [
     {"kind": "env", "name": "SGLANG_ENABLE_OVERLAP_PLAN_STREAM", "type": "bool", "default": false,
      "choices": [], "help": "…", "help_source": "docs", "where": "…",
      "level": "high", "when": "speculative decoding on"}]}
```

- `level` is `high`, `mid`, `low` or `unknown`: a hand-written recommendation
  of what is worth a trial, best first. Most entries are `unknown`: debug, CI
  and switches for other hardware. An entry labeled only for other hardware
  than the session's cards is left out.
- `help_source` says whose words `help` is: `engine` (the image's own help or
  comment), `docs` (the engine's docs at that version), or `source` (the code
  that reads it, when nothing describes it).
- Filters: `kind` (`flag` or `env`), `query` (a substring of the name or the
  help), `level` (the lowest level wanted: `mid` returns high and mid),
  `limit`.
- `status` is `ok`, `partial` (some of it could not be read; `notes` says
  what) or `none` (nothing has been read from this image yet).
- `model_facts` and `model_config_json`: the model's facts and its whole
  `config.json`, once read (the same facts as the manifest's `model.facts`;
  here too because a policy reads the manifest once, and the platform may
  finish reading after the session started).
- The type, default and allowed values of a switch are the engine's own:
  a flag's choices from its parser, a vllm env var's declared choices. A
  string env var the engine declares no choices for carries `reads`: the
  engine code that reads it, its own check of the value first.

With a vocabulary for the image, a flag the config renders to that the
image's parser does not accept is refused (`unknown_flag`, with the nearest
spellings it does accept), instead of a launch that fails at start.

**Model facts (1.5).** The manifest's `model.facts` is read from the model's
own `config.json`, where the campaign's runs mount the weights: `layers`,
`kv_heads`, `head_dim`, `max_context`, `experts` and `experts_per_token`,
`shared_experts`, `draft_layers` (built-in MTP/NEXTN layers a speculative
decoder can draft with), `layer_types` (e.g. linear vs full attention),
`kv_lora_rank` (MLA), `dtype`, `quantization`, `rope_scaling`, `multimodal`.
A field the config does not have is absent, never guessed. Empty until read.

### Diagnostics (1.3)

When the manifest's `services.diagnostics` is true. Everything the platform
records about your own launches and benchmarks, to read back — the evidence a
search learns from:

```http
POST /api/policy/v1/configs/check
{"engine_args": {"tp_size": 2, "quantization": "fp8"}}
→ {"ok": false, "config": {…canonical…}, "cards": 2,
   "command": ["--quantization", "fp8", "--tp-size", "2"],
   "problems": [{"kind": "forbidden", "param": "quantization", "message": "…"}],
   "warnings": [], "deviations": [{"param": "quantization", "kind": "unknown", …}]}
```

A dry run: the canonical echo, the engine arguments it renders to, the
environment it sets (`env`, 1.5), and everything a launch would refuse
(`problems`: `placement`, `forbidden`, `not_renderable`, `round_trip`,
`bad_key`, `unknown_env`, `unknown_flag`, `safety`), without spending one.
Never `422`s. `warnings` refuse nothing: flags the platform's catalog does not
know (the engine may still accept them; a typo only fails at launch).

```http
GET /api/policy/v1/launches/31/log?tail=500&grep=out%20of%20memory&context=5
```

The engine's log, as plain text: live from its container while it runs, the
saved copy after. `grep` is a case-insensitive regular expression (or a plain
substring if it is not one), `context` lines either side of each match.
Startup errors, OOM traces and the engine's own throughput lines are here.

```http
GET /api/policy/v1/launches/31/environment
→ {"engine_version": "0.5.3", "torch_version": "2.8.0", "cuda_version": "12.8",
   "image_digest": "lmsysorg/sglang@sha256:…", "card_type": "H100", …}
```

What the launch actually ran on, recorded once its engine is up. Copy it into
a contender's `launch_spec.environment`: that is what makes a verdict
promotable.

```http
GET /api/policy/v1/benchmarks/57/results
GET /api/policy/v1/benchmarks/57/results/raw
```

The benchmark in full: every scenario, each concurrency level's metrics
(throughput per card, TTFT and ITL percentiles, …), the best level that met
the gate, and `status` (`measured`, `failed`, `running`, `queued`). `/raw` is
the benchmark platform's own payload as harvested (`409` until there is one).
These are the same documents the agent report API serves to people.

### Events

```http
POST /api/policy/v1/events
{"level": "info", "message": "round 3: promoted config a91f to contender rank 1"}
```

Shows up on the campaign's session timeline. stdout is captured too, but only
into the session log; events are the curated channel.

---

## End-to-end sequence

```
platform: container up ──► you: manifest, handshake, plan
   SEARCHING   your loop: propose → launch/bench (platform or self) → trials → contenders
   ...heartbeat every 30 s, command "run"...
   05:45  command "finalize" ──► you: stop, tear down, POST /session/finalized
   VALIDATING  command "serve" (rank 1, port 28200) ──► you serve it ──► POST …/serving
               platform: health check → full replay → verdict recorded
               command "serve" (rank 2, port 28200) ──► same
   DONE   command "exit" ──► you exit 0; container removed; production restored
```

The session fails — rather than finishing — if your container dies or wedges
mid-search; registered contenders are then validated by platform re-launch
from their specs, time permitting. Either way the machine's own guarantees
(production restored before hand-back) are the platform's, not yours.

---

<a name="conventions"></a>
## Conventions

- **Idempotency**: send an `Idempotency-Key` header (unique within the
  session) on `POST /launches`, `POST /benchmarks`, `POST /trials`. Retrying
  the same key returns the original result; a retrying policy never
  double-launches.
- **Latency**: the worker acts on requests on its next tick (`time.tick_s`,
  10 s). Launch/bench state changes are that granular.
- **Timestamps** are UTC ISO-8601. **No `inf`** anywhere: an unmeasurable
  redline slack is `null`.
- **Errors**:

| Status | Meaning | What to do |
| --- | --- | --- |
| `401` | Bad or expired token | The session is over for you; exit. |
| `409` | The phase forbids it (launch after finalize, bench that cannot finish before the deadline, contender update after freeze, `serving` for a contender that was not commanded) | Re-read the heartbeat command and follow it. |
| `410` | Session reached a terminal state | Exit. |
| `422` | Config rejected: safety (cards, malformed values), a forbidden parameter, or a value that cannot round-trip through a command line — the message says which | Fix the config; every config faces the same validation. `POST /configs/check` tells you without trying. |
| `429` | No free cards/ports inside your allocation | Release something, or wait. |

---

<a name="evolution"></a>
## Evolution

- **1.1** (additive, non-breaking): the heartbeat gained a machine-read
  `status` signal (`exhausted` / `error`) and a `coverage` report, and the
  platform gained the productivity watchdog (`search_idle_timeout_s`,
  `search_idle_strikes`) and a recorded `search_end_reason`. A 1.0 image keeps
  working unchanged — it simply never sends the new fields, and the deadline
  plus the watchdog govern it exactly as before.
- **1.7** (additive): `production` is the platform's production config —
  built from the Baselines record or, with none, from the campaign's recipe —
  with `env`, `image`, `config` and `source` beside `engine_args`, and
  `services.production_config` on the menu. `engine_args` now lists every
  flag production runs, not only the recorded ones. A forbidden parameter at
  the value the campaign's recipe sets counts as unchanged, like one at the
  space's base. A 1.6 policy runs unchanged.
- **1.6** (additive): [recipes](#config-format). `model.recipe` in the
  manifest and `services.recipes` on the menu; `prefill.`, `decode.` and
  `values.` config addresses; `recipe`, `shape` (pods and cards) and `commands` (each
  role's engine argv as the chart builds it) on the `/configs/check` answer.
  New refusals only for the new addresses. A 1.5 policy runs unchanged.
- **1.5** (additive): `env.<NAME>` config keys and `GET /engine/vocabulary`,
  with `services.env_keys` and `services.vocabulary` on the menu, `env` on
  the `/configs/check` answer, and `model.facts` in the manifest. One new
  refusal: with a vocabulary for the image, a flag its parser does not take
  (`unknown_flag`), which would have failed the launch anyway.
- **1.4** (additive): `PUT /session/digest` and `GET /history`, and
  `services.history` on the menu. A 1.3 policy runs unchanged.
- **1.3** (additive): diagnostics (`POST /configs/check`, launch logs and
  environments, full benchmark results), [measures](measures.md) on every
  benchmark plus the catalogue and each suite's workload in the manifest,
  `policy.params`, and forbidden
  parameters plus the command-line round trip. The last two are new
  refusals: a 1.2 policy that never changes a forbidden parameter or sends a
  list value runs unchanged, and one that does now gets a `422` that names
  the parameter.
- **1.2** (additive, non-breaking): Kubernetes node pools. The manifest's
  `hardware` gained `pool`, `node_count` and `node_gpu_max`, and a launch can
  report `queued` again, with `waiting_reason` and `waiting_since`, while the
  cluster has no room for it. A 1.1 image keeps working: it treats `queued` as
  "not serving yet" and waits, as it did before.
- The prefix is versioned (`/api/policy/v1`); the manifest carries
  `contract.version`. Additions — new endpoints, new manifest fields, new
  `services` entries — are minor and non-breaking. Breaking changes ship as
  `/v2` alongside `/v1`.
- **Both sides ignore unknown JSON fields.** Build your policy that way; the
  SDK already does.
- New heartbeat commands are sent only to policies whose handshake declared
  the matching capability. An image built against contract 1.0 keeps working
  untouched.
- Planned service entries you may see appear on the menu over time:
  additional benchmark suites (the platform's mini replay), `datasets`
  (bring-your-own-dataset for *search* — verdicts stay on the platform-pinned
  dataset), `secrets` (scoped credentials for LLM-agent policies).

## Guarantees

1. **The verdict is the platform's measurement**, on the platform-pinned
   verify dataset, of what you served. Nothing you report ranks a leaderboard.
2. **Deadlines include the reserve**: `search_deadline` always leaves the reserved
   validation time before `hard_deadline`; both arrive on every heartbeat.
3. **Your configs are never silently rewritten** — canonicalized and echoed,
   deviations badged, safety rejections explained.
4. **Registered contenders survive you.** Crash, wedge, or be aborted: what
   `PUT /contenders` last said is what the platform tries to validate.
5. **One session, one token, one machine.** Your token cannot affect anything
   beyond your own night.

## What is *not* guaranteed

- That every contender gets validated — a night that runs out of clock
  validates in rank order (production first, then the contenders, side by
  side as the budget allows) and marks the rest `skipped`.
- That a benchmark you request completes — the window's hard cutoff kills
  running work, and `EvaluatorBusy` nights exist. Design your loop to treat a
  lost measurement as a lost measurement, not a crisis.
- Sub-tick latency, or inbound connectivity to your container.

---

## Related

- `/api/docs` on your install — every endpoint with schemas.
- [Machine Lease API](machine-lease.md) — how the machine itself is leased to
  the platform; a policy session always lives inside a lease and a campaign
  window.
- The SDK in [llm-autotune-policies](https://github.com/modelsphere/llm-autotune-policies) (`autotune_policy/`)
  implements everything in this document — `run_policy(SearchLoop(your_algorithm))`
  satisfies every MUST — and its `random-search/` is both the reference policy
  and the copy-and-edit starting point. That repository is checked out here
  under `policies/` as a submodule.
