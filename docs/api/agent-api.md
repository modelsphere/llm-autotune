# Agent ↔ Platform API (report generation)

Routes live under `/api/agent/v1/` (backend `app/api/agent.py`, read models in
`app/agent/`), and saved reports render at `/reports/{id}` in the UI. The
sections below are the contract.

An **agent** is an LLM (Claude Code today, an MCP client or a platform-hosted
agent later) that turns finished runs into a performance report: one
**baseline** launch config compared against N **attempts**, in the style of a
"performance lab" write-up. The attempts are runs of a campaign: whatever the
platform launched and measured, whether a policy proposed it or the campaign's
declared space did.

This document is the contract. Exact request/response shapes will be at
[`/api/docs`](/api/docs) under the `agent` tag once implemented.

---

## Design rules

These are the decisions; everything below follows from them.

1. **Runs are the unit.** Every measured thing on the platform is a run: a
   candidate is a run, a verification is a run, a baseline canary is a run.
   The agent API addresses runs by id and nothing else. A campaign is context
   around a run, not an alternative identity for it.

2. **One schema, two levels of aggregation.** There are five *parts*
   (launch, environment, benchmark, results, log pointer) and three *documents*
   that embed those parts unchanged (`run`, `campaign`, `comparison`). A part
   fetched on its own is byte-identical to the same part embedded in a document.
   A weak agent reads documents only. A strong agent reads the parts it needs.
   Neither gets a degraded view.

3. **Structured first, rendered second.** Anything that exists as data is
   served as data: the launch config is a `LaunchConfig` object, the benchmark
   is the LLMBench document plus the platform overlay, results are the per
   level metric groups. Rendered forms (the docker command, markdown tables)
   are served *alongside* the data as conveniences, never instead of it. The
   only free text is the run log, and it is a separate resource.

4. **The platform does arithmetic, the agent does prose.** Deltas, best
   per scenario, comparability, config diffs and verdicts are computed
   server-side and appear once, in the comparison document. The agent never
   derives a percentage or decides whether two runs are comparable.

5. **No spaghetti: nouns, ids, links.** Every route is
   `/api/agent/v1/<noun>[/<id>[/<part>]]`. No verbs in paths except the two
   writes. Every response carries `kind`, `schema_version` and `links` to its
   parts, so an agent that only knows the entry point can discover the rest.

6. **Additive.** The existing `/api/campaigns/*` and
   `/api/runs/*` routes stay as they are. The agent routes are read models over
   the same tables plus one new table for saved reports.

---

## Routes

Read, in the order a weak agent uses them:

| Route | Returns | Who calls it |
|---|---|---|
| `GET /api/agent/v1/campaigns` | campaigns with run counts | everyone, to find ids |
| `GET /api/agent/v1/campaigns/{id}` | `CampaignDocument`: benchmark part + every run's headline | everyone |
| `GET /api/agent/v1/` | entry point: links to everything below | agents that know only the base URL |
| `GET /api/agent/v1/comparison?baseline=RUN&attempts=RUN,RUN,…` | `ComparisonDocument`: the whole report's data. Selectors are run ids | weak agents: this is the report |
| `GET /api/agent/v1/comparison?…&format=markdown` | the same document's tables rendered | weak agents paste these |
| `GET /api/agent/v1/runs/{id}` | `RunDocument`: all five parts | strong agents checking one run |
| `GET /api/agent/v1/runs/{id}/launch` | `LaunchPart` | drill-down |
| `GET /api/agent/v1/runs/{id}/environment` | `EnvironmentPart` | drill-down |
| `GET /api/agent/v1/runs/{id}/benchmark` | `BenchmarkPart` | drill-down |
| `GET /api/agent/v1/runs/{id}/results` | `ResultsPart` | drill-down |
| `GET /api/agent/v1/runs/{id}/results/raw` | LLMBench submission JSON, untouched | strong agents only |
| `GET /api/agent/v1/runs/{id}/log?tail=N` | plain text | explaining a failed attempt |

Write:

| Route | Does |
|---|---|
| `POST /api/agent/v1/reports` | saves markdown + assets against the run ids it was built from; returns a viewer URL |
| `GET /api/agent/v1/reports?campaign_id=ID` | saved reports, newest first |
| `GET /api/agent/v1/reports/{id}` | the saved report, its markdown and the frozen comparison |
| `GET /api/agent/v1/reports/{id}/assets/{name}` | one chart, with its content type |

Errors are JSON, always, with `error` and `reasons[]`:

- `404` unknown run / campaign.
- `409 run_not_finished` for a part that needs results the run has not produced.
  The body says which status the run is in and whether waiting will help.
- `422 not_comparable` from the comparison route, with one reason per
  mismatch (see Comparability). The document is *not* returned partially.

---

## Parts

### LaunchPart

What was started. The structured form is the existing `LaunchConfig`
(`backend/app/control/launch_config.py`) with `engine_args` in the canonical
vocabulary (the flag catalog's names, e.g. `tp`, `mem_fraction_static`, not
`--tensor-parallel-size`). The rendered command is the same one the platform
ran, produced by the same renderer, so it is copy-pasteable.

```json
{
  "kind": "launch", "schema_version": 1, "run_id": 812,
  "config": {
    "engine": "sglang", "image": "sglang:glm-5.3-flash",
    "model_path": "/mnt/models/GLM-5.2", "served_model_name": "glm-5.2",
    "service_port": 28200,
    "engine_args": {"tp": 4, "mem_fraction_static": 0.85, "kv_cache_dtype": "fp8_e4m3"},
    "extra_env": {"SGLANG_ENABLE_DEEPEP": "1"}, "extra_volumes": {},
    "gpu_type": "H200"
  },
  "cards": 4,
  "rendered": {
    "engine_command": "python -m sglang.launch_server --model-path … --tp 4 …",
    "docker_command": "docker run -d --gpus '\"device=0,1,2,3\"' … sglang:glm-5.3-flash …",
    "engine_flags": {"tp": "--tp", "mem_fraction_static": "--mem-fraction-static", "kv_cache_dtype": "--kv-cache-dtype"}
  },
  "origin": {
    "source": "campaign",
    "submission_id": 91, "submission_name": "kv fp8", "version": 1,
    "supersedes_submission_id": 88, "notes": "same as #88 but kv cache fp8",
    "campaign_id": 402, "candidate_id": 1190, "submitted_by": "alice"
  }
}
```

`engine_flags` maps each canonical arg to the engine's real flag, so an agent
can write "`--kv-cache-dtype fp8_e4m3`" in prose without knowing the engine.
`notes` is the human's stated intent for this attempt; the comparison document
relies on it for the narrative.

### EnvironmentPart

What the numbers were produced by. This is `Run.env_snapshot` as recorded by
the version probe plus the machine row, not re-probed.

```json
{
  "kind": "environment", "schema_version": 1, "run_id": 812,
  "machine": {"name": "node-85", "gpu_type": "H200", "gpu_count": 8, "driver": "ssh_docker"},
  "gpu_indices": [0,1,2,3], "card_type": "H200",
  "engine_version": "0.5.15", "torch_version": "2.9.0", "cuda_version": "12.9",
  "gpu_name": "NVIDIA H200", "image_digest": "sha256:…",
  "started_at": "…", "finished_at": "…", "duration_seconds": 3120
}
```

Fields the probe could not read are absent, not empty strings. The agent
must write "engine version not recorded" rather than invent one.

### BenchmarkPart

The benchmark settings, from both owners, with frozen and live kept apart.

- **LLMBench owns** the module list, each module's params (input/output
  tokens, the concurrency grid, requests per level, SLO thresholds, dataset)
  and the `metric_configs` that say which metrics are redlines, display
  thresholds or score inputs.
- **The platform owns** the campaign's overlay: the ranking metric, the SLO its
  objective implies,
  reference, ranking metric, board columns, pricing, the template it was
  created from and the config hash it recorded.
- **Frozen** is what the run actually measured against: each module run's
  locked params and thresholds, as stored on the measurement at harvest time.
  This is authoritative for the report. **Live** is what the benchmark says
  today, which may have been edited since; it is included so the agent can
  say "the benchmark has since changed" and for `drifted`.

```json
{
  "kind": "benchmark", "schema_version": 1, "run_id": 812,
  "llmbench": {
    "slug": "glm-5-2-h200-sweep", "benchmark_id": 71, "url": "https://llmbench…/benchmarks/71",
    "config_hash_frozen": "9f1c…", "config_hash_live": "9f1c…", "drifted": false,
    "dataset_build_id": "prod-traffic-2026-09-10"
  },
  "modules": [
    {
      "key": "perf_guidellm_sweep", "module_name": "perf_guidellm_sweep", "order_index": 0,
      "weight": 1.0, "is_scenario": true, "label": "8k + 1k",
      "params": {"input_tokens": 8000, "output_tokens": 1000, "concurrencies": "1,2,4,8,16",
                 "requests_per_concurrency": 20, "slo_max_ttft_ms": 60000,
                 "slo_ttft_percentile": "p50", "slo_min_request_output_tps": 15, "…": "…"},
      "metric_configs": [
        {"key": "ttft_p99_ms", "role": "redline", "max_val": 120000},
        {"key": "uptime", "role": "redline", "min_val": 0.99},
        {"key": "output_tps", "role": "score", "weight": 1, "formula": "passthrough"}
      ]
    },
    {"key": "perf_guidellm_sweep#2", "module_name": "perf_guidellm_sweep", "is_scenario": true,
     "label": "32k + 100", "params": {"…": "…"}, "metric_configs": ["…"]},
    {"key": "opencompass", "module_name": "opencompass", "is_scenario": false,
     "params": {"datasets": ["gsm8k", "longbench"], "…": "…"}, "metric_configs": ["…"]}
  ],
  "platform": {
    "ranking_metric": "perf_guidellm_sweep.output_tpm_card_norm",
    "slo": {"ttft_percentile": "p50", "max_ttft_ms": 60000, "min_request_output_tps": 15},
    "gate": {"metric": "perf_guidellm_sweep.reported_level_meets_slo", "…": "…"},
    "quality_floors": {"opencompass.gsm8k": 0.92},
    "reference": {"run_id": 790, "submission_id": 80},
    "board_columns": [{"key": "perf_guidellm_sweep.ttft_p50_ms", "label": "TTFT p50"}],
    "pricing": {"display": "usd", "usd": {"input_per_m": 0.6, "output_per_m": 2.2}}
  }
}
```

`modules[].params` are the **frozen** ones. Module keys use the platform's
instance grammar: a second run of the same module is `name#2`. The same key
prefixes every metric in `ResultsPart`, so the agent can join modules to
results without guessing.

### ResultsPart

What was measured, at four resolutions, all from the one stored LLMBench
submission and the one stored measurement row. Nothing is recomputed.

```json
{
  "kind": "results", "schema_version": 1, "run_id": 812,
  "status": "measured",                       // measured | failed | running | queued
  "passed": true, "feasible": true, "score": 1.42, "objective_value": 24400.0,
  "headline": {
    "ranking_metric": "perf_guidellm_sweep.output_tpm_card_norm", "ranking_value": 24400.0,
    "total_tpm_card_norm": 31000.0, "output_tpm_card_norm": 24400.0,
    "peak_concurrency": 8, "reported_concurrency": 8, "meets_gate": true,
    "quality": {"opencompass.gsm8k": 0.94}
  },
  "scenarios": [
    {
      "key": "perf_guidellm_sweep", "label": "8k + 1k",
      "input_tokens": 8000, "output_tokens": 1000,
      "summary": {"tpm_card_norm": 31000.0, "output_tpm_card_norm": 24400.0,
                  "concurrency": 8, "request_output_tps": 50.8, "ttft_ms": 812,
                  "meets_gate": true, "passed": true},
      "levels": [
        {"concurrency": 1, "metrics": {"output_tps": 61.2, "request_output_tps": 61.2,
                                       "ttft_p50_ms": 310, "ttft_p90_ms": 380, "ttft_p99_ms": 450,
                                       "tpot_p50_ms": 16.1, "itl_p99_ms": 31.0,
                                       "http_status_200": 20, "measured_requests": 20}},
        {"concurrency": 2, "metrics": {"…": "…"}},
        {"concurrency": 16, "metrics": {"…": "…"}}
      ]
    },
    {"key": "perf_guidellm_sweep#2", "label": "32k + 100", "summary": {"…": "…"}, "levels": ["…"]}
  ],
  "quality": [
    {"key": "opencompass", "scores": {"gsm8k": 0.94, "longbench": 0.61}, "passed": true}
  ],
  "verdicts": [
    {"module": "perf_guidellm_sweep", "metric": "ttft_p99_ms", "role": "redline",
     "min": null, "max": 120000, "actual": 4100, "ok": true},
    {"module": "perf_guidellm_sweep", "metric": "uptime", "role": "redline",
     "min": 0.99, "max": null, "actual": 1.0, "ok": true},
    {"module": "opencompass", "metric": "gsm8k", "role": "quality_floor",
     "min": 0.92, "max": null, "actual": 0.94, "ok": true}
  ],
  "modules": [
    {"key": "perf_guidellm_sweep", "status": "done", "passed": true, "score": 1.42, "error": ""},
    {"key": "opencompass", "status": "done", "passed": true, "score": 0.9, "error": ""}
  ],
  "metrics": {"perf_guidellm_sweep.output_tps": 406.4, "perf_guidellm_sweep.c8.ttft_p50_ms": 812, "…": "…"},
  "failure": null,                            // or {"class": "engine_crash", "error": "…", "log": "/api/agent/v1/runs/812/log"}
  "links": {"raw": "/api/agent/v1/runs/812/results/raw", "log": "/api/agent/v1/runs/812/log"}
}
```

Resolutions, from coarse to fine:

- `headline` is what the campaign's leaderboard ranks on. One number per concept.
- `scenarios[].summary` is the board's per-scenario row (the existing
  `scenarios_of`), at the concurrency the sweep reported.
- `scenarios[].levels[]` is the per-concurrency group from the raw
  submission (`c1`, `c2`, …), the full metric set at that level. This is
  what the report's tables and charts are made of.
- `metrics` is the flat dict exactly as stored (`module.level.metric`), for
  agents that already know a key.
- `raw` is the LLMBench payload, untouched, behind a link rather than inline
  because it is large and the agent rarely needs it.

`verdicts` merge three sources into one shape: LLMBench redlines and display
thresholds (from `module_reports.bounds`), and the platform's confirmed quality
floors. `ok: null` means unmeasured, not passed.

A failed run has `status: failed`, empty `scenarios`, and a `failure` block
with the platform's failure class, the error's first line and a link to the
log. Failed attempts are part of a report, so this part exists for them too.

---

## Documents

### RunDocument

`GET /api/agent/v1/runs/{id}`. All five parts under their own keys plus
identity and links. No field appears here that is not in a part.

```json
{
  "kind": "run", "schema_version": 1, "run_id": 812,
  "launch": {"kind": "launch", "…": "…"},
  "environment": {"kind": "environment", "…": "…"},
  "benchmark": {"kind": "benchmark", "…": "…"},
  "results": {"kind": "results", "…": "…"},
  "links": {"launch": "…/launch", "environment": "…/environment", "benchmark": "…/benchmark",
            "results": "…/results", "raw": "…/results/raw", "log": "…/log"}
}
```

### CampaignDocument

`GET /api/agent/v1/campaigns/{id}` — the campaign's benchmark (frozen from its
most recently measured run) and one row per run, each with the run id a
comparison is built from, its config, and its headline numbers. Runs that
failed or never reached the benchmark are listed too, with their status: an
attempt that did not work is part of the story a report tells.

`baseline_run_id` names the run of the campaign's baseline config, when it ran
one — the natural `baseline=` for a comparison.

### ComparisonDocument

`GET /api/agent/v1/comparison?baseline=790&attempts=801,805,812`. The report's
data in one response. `attempts` order is the ablation order: each attempt
carries a diff and deltas against the baseline **and** against the previous
attempt in the list, so both a "baseline vs optimized" table and a
"one knob at a time" narrative can be written from it.

```json
{
  "kind": "comparison", "schema_version": 1,
  "comparable": true, "reasons": [],
  "benchmark": {"kind": "benchmark", "…": "…"},        // frozen params of the BASELINE run; attempts verified equal
  "baseline": {"run_id": 790, "launch": {"…": "…"}, "environment": {"…": "…"}, "results": {"…": "…"}},
  "attempts": [
    {
      "run_id": 812, "position": 3,
      "launch": {"…": "…"}, "environment": {"…": "…"}, "results": {"…": "…"},
      "diff_vs_baseline": {
        "engine": {"from": "vllm", "to": "sglang"},
        "image": {"from": "vllm:0.11", "to": "sglang:glm-5.3-flash"},
        "engine_args": {"added": {"kv_cache_dtype": "fp8_e4m3", "mem_fraction_static": 0.85},
                        "removed": {}, "changed": {}},
        "extra_env": {"added": {"SGLANG_ENABLE_DEEPEP": "1"}, "removed": {}, "changed": {}},
        "cards": {"from": 4, "to": 4}
      },
      "diff_vs_previous": {"engine_args": {"changed": {"kv_cache_dtype": {"from": "bf16", "to": "fp8_e4m3"}}}, "…": "…"},
      "deltas": {
        "headline": {"ranking_value": {"baseline": 15200.0, "attempt": 24400.0, "pct": 60.5},
                     "peak_concurrency": {"baseline": 4, "attempt": 8, "pct": 100.0}},
        "scenarios": [
          {"key": "perf_guidellm_sweep", "label": "8k + 1k",
           "summary": {"output_tpm_card_norm": {"baseline": 15200.0, "attempt": 24400.0, "pct": 60.5},
                       "ttft_ms": {"baseline": 1200, "attempt": 812, "pct": -32.3, "better": "lower"}},
           "levels": [
             {"concurrency": 1, "metrics": {"output_tps": {"baseline": 48.0, "attempt": 61.2, "pct": 27.5},
                                            "ttft_p50_ms": {"baseline": 400, "attempt": 310, "pct": -22.5, "better": "lower"}}},
             {"concurrency": 8, "metrics": {"…": "…"}}
           ]}
        ],
        "quality": {"opencompass.gsm8k": {"baseline": 0.93, "attempt": 0.94, "pct": 1.1}},
        "vs_previous": {"headline": {"ranking_value": {"previous": 22100.0, "attempt": 24400.0, "pct": 10.4}}}
      },
      "verdict_changes": [{"metric": "perf_guidellm_sweep.ttft_p99_ms", "baseline_ok": false, "attempt_ok": true}]
    }
  ],
  "best": {
    "overall": {"run_id": 812, "by": "ranking_metric"},
    "per_scenario": [{"key": "perf_guidellm_sweep", "run_id": 812}, {"key": "perf_guidellm_sweep#2", "run_id": 805}]
  },
  "series": [
    {"scenario": "perf_guidellm_sweep", "metric": "output_tps", "x": "concurrency", "unit": "tok/s", "better": "higher",
     "lines": [{"run_id": 790, "label": "baseline: vllm default", "points": [[1, 48.0], [2, 90.1], [4, 160.0]]},
               {"run_id": 812, "label": "kv fp8", "points": [[1, 61.2], [2, 118.0], [4, 230.0], [8, 406.4]]}]},
    {"scenario": "perf_guidellm_sweep", "metric": "ttft_p50_ms", "x": "concurrency", "unit": "ms", "better": "lower", "lines": ["…"]}
  ],
  "catalog": {
    "output_tpm_card_norm": {"label": "Output tok/min per GPU", "unit": "tok/min/GPU", "better": "higher", "help": "…"},
    "ttft_p50_ms": {"label": "TTFT p50", "unit": "ms", "better": "lower", "help": "…"}
  }
}
```

Notes on the comparison:

- `pct` is signed relative change `(attempt - baseline) / baseline × 100`.
  `better` says which direction is an improvement, taken from the metrics
  catalog, so a weak agent never has to know that TTFT going down is good.
- `series` is chart-ready: one entry per (scenario, metric), one line per
  run, points already aligned on the concurrency axis. A run that has no
  value at a level simply has no point there.
- `catalog` carries label, unit, direction and one-paragraph help for every
  metric key that appears anywhere in the document. Prose about a metric
  should use the catalog's label and unit.
- `format=markdown` returns the same document with `rendered` added: one
  markdown table per scenario per level set, a headline table, a config diff
  table per attempt, and the launch commands as fenced blocks. Values are
  formatted once, server-side, so every report shows the same precision.

#### Comparability

The comparison route refuses (422) unless, for the baseline and every
attempt:

- same model, served name and GPU type;
- same LLMBench benchmark slug and same **frozen** module params for every
  scenario module (input/output tokens, concurrency grid, requests per level,
  SLO thresholds);
- same dataset build id when the benchmark pins one;
- results status is `measured` or `failed` (not running or queued).

Card count is **not** a comparability condition: comparing tp=2 against tp=4
is a legitimate report. Card-normalized metrics are the headline for that
reason, and `cards` appears in every diff so the agent can say so.

Each reason is a structured line:

```json
{"code": "module_params_differ", "run_id": 805, "module": "perf_guidellm_sweep",
 "field": "concurrencies", "baseline": "1,2,4,8,16", "attempt": "1,2,4,8,16,32"}
```

`?force=true` returns the document anyway with `comparable: false` and the
reasons filled in, for a human who knows what they are doing. The saved
report records the flag.

---

## Reports

`POST /api/agent/v1/reports`

```json
{
  "title": "Optimizing GLM-5.2 throughput on H200",
  "baseline_run_id": 790, "attempt_run_ids": [801, 805, 812],
  "comparable": true,
  "markdown": "# …",
  "assets": [{"name": "output_tps_8k_1k.png", "content_type": "image/png", "data_base64": "…"}],
  "generator": {"agent": "claude-code", "model": "claude-opus-5", "template": "perf-lab-v1"}
}
```

Returns `{id, url, created_at}`. The report row stores the run ids and the
comparison document as it was at generation time, so a report can be re-read
years later even if the benchmark drifted. `GET /api/agent/v1/reports/{id}`
returns all of it. The frontend renders markdown at `/reports/{id}` and
serves the assets by relative name.

---

## Conventions the agent must follow

These live in the agent's skill file, but they are part of the contract
because the API is shaped around them.

- **Metric key grammar** is `<module key>.<level>.<metric>` in `metrics`,
  where level is `c1`, `c2`, … and is absent for module-level metrics. A
  second instance of a module is `name#2`. In `scenarios[].levels[]` the
  same values appear without the prefix, under `concurrency`.
- **Use the catalog** for names, units and direction. Do not paraphrase a
  metric's meaning beyond its `help` text.
- **Copy deltas, never compute them.** If a number is not in `deltas`, it is
  not in the report as a comparison.
- **Card-normalized metrics** are the headline whenever attempts differ in
  `cards`. Absolute metrics may be shown alongside, labelled as absolute.
- **Failed attempts stay in the report**, with `failure.class` and the log
  tail, under the knob they were testing.
- **Do not compare across a 422.** If `comparable` is false, the report says
  so up front and why.
- **Environment fields that are absent are reported as not recorded.**
- **Charts come from `series`** through the bundled plotting script, not
  from reading tables.

---

## Auth and scope

The agent authenticates with `X-API-Key`. A key acts as its owner, so the
agent's key is minted from a dedicated non-admin user (`reporter`). That user
can read everything above, save reports and, in phase 1b, submit entries; it
cannot change a campaign's objective or its benchmark. The audit trail shows
`key:reporter`. A `read_only` flag on keys is the next step if agents ever get
more autonomous than that.

The MCP server, when it comes, exposes exactly the routes above as tools with
the same names (`get_run`, `get_comparison`, `save_report`, …) and the same
JSON. It is a wrapper, not a second API.

---

## Expandability and escape hatches

The agent API is the recommended path, not the only one. Three tiers, from
preferred to discouraged:

1. **Agent API** (`/api/agent/v1/*`). Stable shapes, computed comparisons,
   documented conventions. Everything a report needs should be here.
2. **Existing platform API** (`/api/campaigns/*`,
   `/api/runs/*`, `/api/openapi.json`). Same `X-API-Key` works. A strong agent
   may use it when the agent API lacks a field. The skill file lists these
   routes as "allowed, not preferred" and forbids the mutating ones except
   entry submission.
3. **Direct database queries.** Possible, not recommended, and
   never from a container we ship. If a human wants it for a one-off, use a
   read-only Postgres role (`agent_ro`, SELECT only). Schemas are internal
   and change without notice.

**Promotion rule.** Any fallback the agent uses twice becomes a feature
request: add the field or part to the agent API, then remove the fallback from
the skill. The fallbacks exist so the first week is not blocked on us, not as a
permanent second interface.

**Versioning.** `/v1/` in the path, `schema_version` on every part and
document. Within v1 changes are additive only: new keys, new parts, new query
selectors. Agents must ignore unknown keys. Removing or renaming a field is a
`/v2/`.

**Adding a part.** A new part (for example `cost`, or `search` for autotune)
is one Pydantic model, one route, one key in `RunDocument` and
`ComparisonDocument`, and one entry in `links`. Nothing else changes.

**Other agents.** The routes are plain JSON over HTTP with a header key, so
any harness (Claude Code, an MCP client through the wrapper, a cron job, the
frontend itself) can consume them. The skill file is the only Claude-specific
artifact.

---


## Implementation notes

- `LaunchPart` is `LaunchConfig.from_run` plus the existing renderers in
  `backend/app/control/launch/ssh_docker.py` and `engines/flags.py`.
- `EnvironmentPart` is `Run.env_snapshot` plus the machine row.
- `BenchmarkPart.modules[].params` come from the measurement's
  `module_reports[].params` (frozen); live comes from the existing
- `ResultsPart.scenarios[].levels[]` come from `Result.raw` (the `cN` groups
  per module run), `summary` from `board.scenarios_of`, `verdicts` from
  `module_reports` plus `quality_of`.
- `ComparisonDocument.catalog` is `metrics_catalog`.
- New table: `agent_reports` (id, title, baseline_run_id, attempt_run_ids,
  comparable, markdown, comparison_json, assets, generator, created_by,
  created_at). One migration.
- Rough effort: parts + run document 1 day, comparison 1 day, markdown
  rendering half a day, reports table + viewer half a day.

---

