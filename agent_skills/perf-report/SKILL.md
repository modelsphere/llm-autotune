---
name: perf-report
description: Write a performance report (baseline vs optimized launch configs) from finished LLM AutoTune runs, using the platform's agent API. Use when asked to write, generate or update a perf report for a campaign, or to compare runs.
---

# perf-report

You write a performance-lab style report: one **baseline** launch config against
one or more **optimized attempts**, for one model on one GPU type, in English and
in Chinese. The platform has already run and benchmarked everything, done the
arithmetic, and draws every chart, table and serving command itself. Your job is
the prose and the order of things.

Before you read any data, read "Three ideas the whole report rests on" at the
top of `TEMPLATE.md`: normalized throughput (to 8 GPUs) as the only throughput,
what meeting the SLO means, and the best concurrency within SLO. Most wrong reports get one
of those three wrong.

## Setup

Environment variables, set by whoever runs you:

```
AUTOTUNE_URL       the platform API, e.g. https://autotune.example.com
AUTOTUNE_API_KEY   an API key minted on the platform (sent as X-API-Key)
AUTOTUNE_UI_URL    optional: the web UI, e.g. https://autotune.example.com, for full links
```

Everything goes through `scripts/autotune_report.py` (Python 3.10+, standard
library only). Run it with `python3`.

## The whole job, in order

1. **Find the runs.** The person names a campaign and says which runs are
   the baseline and which are the attempts (for example "A is the baseline, C
   and D are the attempts"). Anything they did not name does not exist for
   this report.

   ```
   python3 scripts/autotune_report.py campaigns
   python3 scripts/autotune_report.py campaign <id>
   ```

   The campaign listing gives each run's id, status, config and headline.
   Run ids are the selectors.

2. **Fetch the comparison.** Attempts go in the order the report should tell
   them (the ablation order).

   ```
   python3 scripts/autotune_report.py compare --baseline 41 --attempts 47,52 --out work/
   ```

   It starts `work/` empty — anything an earlier report left there is moved
   to `work/.previous/`, which you never open — then writes
   `work/comparison.json` (the facts, for your prose) and `work/blocks.md` (every chart/table/command block these runs can draw, ready
   to paste). If the platform says the runs are not comparable it prints the
   reasons and stops. Do not pass `--force` unless the person tells you to, and
   then say so in the report. If it prints a `DATA GAP` line, a sweep has a
   verdict but no levels: keep that scenario's section and chart, say in one
   sentence of its paragraph that no levels were measured, and tell the
   person — do not read numbers into it.

3. **Name the scenarios.** Write `work/scenarios.en.json` and
   `work/scenarios.zh.json`: per scenario key, the `name` the report calls it
   and a one-sentence `description` of what that workload stands for
   (`TEMPLATE.md` → "Naming the scenarios"). They go with the save, so every
   chart, table and heading uses the same name.

4. **Write `work/report.en.md` and `work/report.zh.md`** following `TEMPLATE.md`
   (next to this file) exactly — its structure, its style rules and its
   glossary. Charts, tables and commands are blocks pasted from
   `work/blocks.md`; the numbers in your prose come from
   `work/comparison.json`.

5. **Check both.** Every block must resolve, and there must be no caption you
   wrote yourself (the platform draws them); fix what it names and check again.

   ```
   python3 scripts/autotune_report.py check --markdown work/report.en.md \
       --comparison work/comparison.json --lang en --scenarios work/scenarios.en.json
   python3 scripts/autotune_report.py check --markdown work/report.zh.md \
       --comparison work/comparison.json --lang zh --scenarios work/scenarios.zh.json
   ```

6. **Save both** — English first, then Chinese as its translation, using the id
   the first save printed. Pass `--labels` on both if the person named the
   configs (baseline first).

   ```
   python3 scripts/autotune_report.py save --title "<English title>" \
       --markdown work/report.en.md --comparison work/comparison.json --lang en \
       --scenarios work/scenarios.en.json
   python3 scripts/autotune_report.py save --title "<Chinese title>" \
       --markdown work/report.zh.md --comparison work/comparison.json --lang zh \
       --scenarios work/scenarios.zh.json --translation-of <id printed above>
   ```

   Each prints the report link and its self-contained HTML export. Give both
   reports' links to the person.

## Where each fact lives in `comparison.json`

For prose only — never tabulate these; the blocks do that.

| You need | Read |
|---|---|
| model, GPU type | the run's `environment` part |
| cards per server | `baseline.launch.cards` |
| what changed vs baseline | `attempts[].diff_vs_baseline` (spell a knob as `launch.rendered.engine_flags` does) |
| the person's intent for an attempt | `attempts[].launch.origin.notes` |
| per scenario, at the best concurrency within SLO | `*.results.scenarios[].best_level` (`concurrency`, `total_tps_per_machine`, `request_output_tps`, `ttft_ms`, …), change in `attempts[].deltas.scenarios[].best_level` — each change is `{baseline, attempt, pct, better, improved}` |
| normalized throughput | `best_level.total_tps_per_machine`, its change in `deltas…best_level.total_tps_per_machine.pct` — the numbers `compare` prints. Never a `*_per_gpu` field |
| where the SLO stops a sweep | `*.results.scenarios[].levels[]` — `concurrency`, `metrics.meets_slo` and the metric that failed (`ttft_<pct>_ms`, `request_output_tps`); quote latency and per-request speed from here, never a `levels[]` throughput (it is unnormalized) |
| GPUs per instance | `*.launch.cards` — for the 1.2 sentence only, never to scale a number |
| the Agentic dataset (`scenarios[].kind == "replay"`) | `*.results.scenarios[].metrics`, changes in `attempts[].deltas.scenarios[].metrics` |
| quality scores | `*.results.headline.quality`, changes in `attempts[].deltas.quality` |
| the SLO | `benchmark.platform.slo` |
| a failed attempt | `attempts[].results.status == "failed"`, `results.failure` |

## Rules

- Only the baseline and the named attempts appear in the report.
- Every percentage in prose comes from a `pct` field and every value from the
  document; you never do arithmetic on them (the percentage points of a quality
  change are the one exception). If a number is not there, it is not in the
  report.
- Read only files you wrote in this session and the ones `compare` wrote.
- `improved` tells you whether a change is better; do not reason about whether
  lower TTFT is good, read it.
- A failed attempt is a section, not an omission.
- `TEMPLATE.md`'s rules win over anything you think a reader might like to see.
