# Policy Session API

How an external **policy** — a container image holding a search algorithm —
runs a night of tuning on a machine LLM AutoTune manages, and how the platform
delivers its verdict.

This document is the narrative version: what the calls mean, and what each side
guarantees. For the one-page cheat sheet see [policy-api.md](policy-api.md)
(中文：[policy-api.zh.md](policy-api.zh.md)); the interactive reference that
always matches the running build is **`/api/docs`**, section *policy sessions*.
For real responses from a finished session see the
Contract version: **1.1** (`/api/policy/v1`; see [Evolution](#evolution)).

---

## The contract in one paragraph

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
measurement — never your own — is the campaign's verdict. Then you exit, the
container is removed, and production is restored exactly as it is after any
other night.

---

## Who dials whom

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
  "policy": {"name": "tpe-qwen36", "image": "registry/tpe-qwen36:1.3.0", "version": "1.3.0"},

  "model": {
    "engine": "sglang",
    "image": "lmsysorg/sglang:v0.5.3",
    "model_path": "/model",
    "served_model_name": "qwen36",
    "extra_env": {},
    "extra_volumes": {}
  },

  "hardware": {
    "machine": "gpu-h100-1",
    "gpu_type": "H100",
    "gpu_indices": [0, 1, 2, 3],
    "ports": [28200, 28201, 28202, 28203],
    "gpus_visible_in_container": true
  },

  "objective": {
    "screen":  {"target_metric": "perf_guidellm_sweep.output_tpm_card_norm", "direction": "max", "redlines": []},
    "verify":  {"target_metric": "replay.output_tpm_card_norm", "direction": "max", "redlines": []}
  },

  "search_space": {"base": {}, "grid": {}, "tied": [], "range": {}, "conditions": {}},
  "space_policy": {"deviations": "allowed_if_declared"},

  "production": {"engine_args": {}, "launch_command": "docker run …"},

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
      "screen": {"slug": "autotune-screen-v1", "approx_minutes": 10, "dataset_build_id": "b-20260818"}
    },
    "state": true
  },

  "prior": {"contenders": []}
}
```

Field notes:

- **`hardware`** — the cards and host ports that are yours tonight. Use no
  others. Engines you serve yourself must bind `0.0.0.0:<one of these ports>`
  (the container runs on the host network). If `gpus_visible_in_container` is
  true the cards are already visible to your process.
- **`search_space`** — the campaign author's declared space, in the platform's
  vocabulary (see [Configs](#configs)). `space_policy` says how strictly it
  binds you: `allowed_if_declared` means you may step outside it — an unknown
  flag, a value off the grid — provided your `plan` says so.
- **`objective`** — per suite: the metric the platform will reduce results to,
  its direction, and the redlines a config must hold. Your trials are scored
  with exactly this, so you never need to re-implement it.
- **`production`** — what serves today, for parity checks and as a warm-start
  hint.
- **`time`** — `search_deadline` is when exploration must stop (validation
  time is already reserved after it); `hard_deadline` is the end of the
  window, when anything still running is killed.
- **`services`** — tonight's menu. Absent entry = unavailable tonight. Never
  assume a service; read the menu.
- **`prior`** — validated contenders and verdicts from this campaign's earlier
  nights, so you can warm-start.

---

## Heartbeat: liveness out, commands in

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

## The plan: honesty about what is tuned

```http
PUT /api/policy/v1/session/plan
{
  "tuning": ["chunked_prefill_size", "max_running_requests", "tp_size"],
  "extras": [
    {"param": "enable_torch_compile", "values": [true, false],
     "reason": "not in declared space; strong prior from sglang release notes"}
  ],
  "notes": "TPE, gamma=0.15, warm-started from 2 prior nights"
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
## Configs: one vocabulary, both directions

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

---

## Services

All optional. What is on tonight's menu is what the manifest's `services`
object lists.

### Delegated launch — "run this engine for me"

```http
POST /api/policy/v1/launches
Idempotency-Key: t14-launch
{"engine_args": {"tp_size": 2, "chunked_prefill_size": 4096}, "gpu_indices": [0, 1], "port": 28201}
```

The platform validates (safety only), launches the campaign's engine image
with your args on your cards, health-gates it, and holds it **serving** until
you release it. Poll:

```http
GET /api/policy/v1/launches/31
→ {"status": "serving", "endpoint_url": "http://198.51.100.51:28201",
   "launch_command": "docker run …", "failure_class": "", "error": ""}
```

`status`: `queued → launching → ready → serving`, or `failed` (with the same
`failure_class` taxonomy campaigns use: `oom`, `bad_config`, engine crash —
an OOM is information, learn from it), then `released` after:

```http
DELETE /api/policy/v1/launches/31
```

Concurrent launches on disjoint card sets are allowed. Expect launch → serving
to take minutes (model load), never seconds.

### Benchmarks — "measure this endpoint"

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
   "metrics": {"perf_guidellm_sweep.output_tpm_card_norm": 1234.5, "…": "…"}}
```

`suite` names an entry from `services.benchmarks`. Tonight's menu tells you
each suite's approximate duration and pinned dataset. (v1 offers `screen`;
the full replay `verify` suite is reserved for the platform's own validation.)
A benchmark whose `approx_minutes` no longer fits before `search_deadline` is
refused with `409` — do not start what cannot finish.

### Trials — tell the platform what you measured

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

### Contenders — your best-so-far, kept current

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

### State — warm-start across nights

```http
PUT /api/policy/v1/state          # opaque blob, ≤16 MB, any content-type
GET /api/policy/v1/state
```

Per campaign, not per session: night 2 reads what night 1 wrote. The platform
never looks inside.

### Events — narrate for the humans

```http
POST /api/policy/v1/events
{"level": "info", "message": "round 3: promoted config a91f to contender rank 1"}
```

Shows up on the campaign's session timeline. stdout is captured too, but only
into the session log; events are the curated channel.

---

## The night, end to end

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
| `422` | Config rejected for safety (cards, malformed values) — message says why | Fix the config; this is the same validation every internal planner faces. |
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
  `diagnostics` (pull engine logs and raw per-module benchmark JSON),
  additional benchmark suites (the platform's mini replay), `datasets`
  (bring-your-own-dataset for *search* — verdicts stay on the platform-pinned
  dataset), `secrets` (scoped credentials for LLM-agent policies).

## Guarantees

1. **The verdict is the platform's measurement**, on the platform-pinned
   verify dataset, of what you served. Nothing you report ranks a leaderboard.
2. **Deadlines are honest**: `search_deadline` always leaves the reserved
   validation time before `hard_deadline`; both arrive on every heartbeat.
3. **Your configs are never silently rewritten** — canonicalized and echoed,
   deviations badged, safety rejections explained.
4. **Registered contenders survive you.** Crash, wedge, or be aborted: what
   `PUT /contenders` last said is what the platform tries to validate.
5. **One session, one token, one machine.** Your token cannot affect anything
   beyond your own night.

## What is *not* guaranteed

- That every contender gets validated — a night that runs out of clock
  validates in rank order and marks the rest `skipped`.
- That a benchmark you request completes — the window's hard cutoff kills
  running work, and `EvaluatorBusy` nights exist. Design your loop to treat a
  lost measurement as a lost measurement, not a crisis.
- Sub-tick latency, or inbound connectivity to your container.

---

## Related

- [`/api/docs`](/api/docs) — every endpoint with schemas.
- [Machine Lease API](machine-lease.md) — how the machine itself is lent to
  the platform; a policy session always lives inside a lease and a campaign
  window.
- The SDK in [llm-autotune-policies](https://github.com/modelsphere/llm-autotune-policies) (`autotune_policy/`)
  implements everything in this document — `run_policy(SearchLoop(your_algorithm))`
  satisfies every MUST — and its `random-search/` is both the reference policy
  and the copy-and-edit starting point. That repository is checked out here
  under `policies/` as a submodule.
