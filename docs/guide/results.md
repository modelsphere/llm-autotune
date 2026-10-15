[User guide](README.md) › **Reading results**

# Reading results

A campaign's page has four tabs: **Leaderboard**, **Runs**, **Configuration**
and **Candidates**. **Report** above them produces a Markdown summary.

## Leaderboard

![A finished campaign's leaderboard](images/campaign-leaderboard.png)

The line at the top restates the objective: the direction, the target metric's
label and its full name, and the redlines, if any.

The bars show every measured config, best first, labeled with the values of
the swept parameters in order (`1 · 0.85` is `tp=1`, `mem_fraction_static=0.85`).
The percentages on the right compare each config with the baseline, or with
the best config when there is no baseline.

The table has one row per measured config; click a row to open the run that
produced its score:

| column | means |
|---|---|
| **Run** | the run that produced the score; its details are on the **Runs** tab |
| **Config** | the values that vary between candidates. The fixed ones are on the **Candidates** tab. |
| **GPU** | the card type it ran on, when known. Yellow when it differs from the baseline's card, which makes the comparison unfair. |
| score | the target metric, with its unit, such as *Output TPM per card (tok/min/GPU)* |
| **vs baseline** | how it compares with production's config measured in the same campaign, such as `+12.0%`. Empty when there is no baseline ([Baselines](baselines.md)). |
| **Redlines** | **held** if the config kept within every redline; **crossed** with which one if not. A crossed config never wins. |

A row tagged **baseline** is production's config, measured the same way in
the same campaign. With a baseline, the bars are green (better than the
baseline), grey (the baseline) or orange (worse):

![A leaderboard with a baseline row](images/baseline-leaderboard.png)

Here `tp=1` matches production's config apart from a flag the mock engine
ignores, so its +0.9% is measurement noise; `tp=2` halves the throughput per
GPU.

### Two boards

Some campaigns show two boards; the top one decides:

- **The policy's finalists, measured again**, in a policy campaign: the configs
  the policy named as its best, launched and measured by the platform itself
  on the campaign's benchmark. **All candidates** below is every config the
  policy tried.
- **Verified on real traffic**, in a campaign with a
  [verify stage](campaigns.md#4-benchmark): the best few configs measured
  again with the second workload. **All candidates** below is the first,
  quicker measurement of every config. Their numbers come from different
  benchmarks, so they are never mixed in one table. A replay row tagged
  **other dataset** was measured on a different sample of the dataset and does
  not compare with the rest.

## Runs

The **Runs** tab lists every run: its status, the failure kind if it failed,
the machine's GPUs it held (`#0 #1`), its endpoint, when it started and how
long it took. In a policy campaign, each trial is one row (**Role** *trial*)
with its benchmark run nested under it, and *finalist* rows are the platform's
final measurements. Click a row for the details:

![A run's details](images/campaign-run-drawer.png)

- **Error** (failed runs, shown first): what went wrong, and the failure kind.
- **Verdict:** the benchmark that measured it, the ranking metric, its score,
  and whether it held the redlines.
- **What the objective looked at:** the target metric and every redline
  metric.
- **Engine command:** the engine's command line for this config, with a
  **Copy** button, followed by the image and weights path.
- **Config:** the full set of engine settings, including fixed ones.
- **Launch command, as applied** (collapsed): exactly what was started. On
  Kubernetes, the applied manifest; over ssh, the `docker run` line.
- **Results:** everything LLMBench reported, under **Metrics**.
- **Captured log:** the engine's output, live while the run is active and its
  end after it finishes. **Download** saves it.

[Runs and failures](runs.md) explains the statuses and failure kinds.

## Configuration

**Configuration** shows the campaign as created: every setting, the launch
extras, the search space and the objective, each with a copy button. Later
edits to the original search space or objective do not change it.

![The Configuration tab](images/campaign-configuration.png)

**Difference from production** lists flags that production's recorded
baseline sets and this campaign does not. The results do not account for
these differences.

## Candidates

**Candidates** lists every config in the search space and its outcome:

| status | means |
|---|---|
| **queued** | waiting for a machine |
| a run state, such as **benching** | being measured now |
| **benchmarked** | measured; its score is on the leaderboard |
| **failed** | every attempt failed; the reason is next to it |
| **stopped** | its run was stopped |
| **rejected** | the static check refused it before launch (for example, more GPUs than any machine has) |

The map at the top shows the space and how its candidates fit on the machines
("3 rounds of 4 cards, widest first"). Click a row to open its run. **Fixed in
every candidate** lists the settings that never change.

![The Candidates tab](images/campaign-candidates.png)

## The report

**Report** summarizes the campaign as Markdown: the objective and model, a
verdict, results against production, configs that crossed a redline,
failures, and suggested next steps. **Copy markdown** copies it to the
clipboard.

![The campaign report](images/campaign-report.png)

The report does not claim a win the data does not support. A lead smaller
than the measurement noise is reported as *no meaningful difference*; when
repeated measurements of the same config disagree by more than the lead, the
result is *unstable*. When production's config was not measured (no
[baseline](baselines.md) for the model), the report says so.

## Comparing campaigns

Two results are comparable only when measured the same way:

- **the same benchmark**: the same workload always maps to the same LLMBench
  benchmark, named on the **Benchmark** step and in each run's details;
- **the same card type**;
- for a replay, **the same dataset sample**.

Results from different workloads are different measurements, even when the
metric has the same name.

## Deploying the winner

Click the top row of the leaderboard. The run details show:

- the **Engine command**, with a **Copy** button: the engine's command line
  for the winning config, as it runs inside the image with the weights at
  `/model`;
- the **Image** and **Weights** path;
- the engine version and image digest, in the results.

Apply these to the serving deployment, by hand or from a pipeline. Then update
the baseline on [Baselines](baselines.md), so the next campaign measures
against the new production config.

### Publishing it to the model catalog

**⋯ ▸ Export to model catalog** turns measured runs into files for
[model-catalog](https://github.com/modelsphere/model-catalog), in its
`catalog.swiss/v2` layout: one variant file per run, `models/<name>/<id>.yaml`,
and the model's `metadata.yaml`.

- **Runs:** every run on the leaderboard is listed. The baseline and the
  winner are ticked to start, as roles *baseline* and *optimized*.
- **Model:** the export looks the model up in the published catalog, by its
  Hugging Face repo id, then by name.
  - **A model the catalog has:** its name and metadata are kept. A variant it
    already has gets its next version, or no file at all if nothing changed;
    the others are added.
  - **A new model:** the form asks for its Hugging Face repo id (`source.hf`),
    which every site finds the weights by. A repo id read off the weights path
    is offered as a suggestion, never filled in for you. The rest of
    `metadata.yaml` is drafted and can be edited.
- **Report:** a tuned variant's result goes into `metadata.yaml` as a
  `tuning` entry (the gain over the baseline, per workload), which the catalog
  wants with the perf report beside it. The export ships the newest saved
  report that compares exactly these runs, or the one picked under
  **Advanced**. Without a report it still exports, with no `tuning` entry and
  a warning: the catalog's check asks for one before the pull request passes.
  A shipped report keeps the measurements and leaves out where they were
  taken: run and campaign names, machines, host paths and links into the
  platform.
- **Advanced:** variant ids, an image tag for a run whose image was named by
  digest only, the default variant, env and host-path volumes, the startup
  budget, `gpuProduct` node labels, and a saved report to ship.
- **Env and mounts:** every env var and mount the run had is listed with what
  the export did with it. The serving recipe is kept (engine and runtime
  settings such as `SGLANG_*`, `VLLM_*`, `TORCHINDUCTOR_*`); this site's setup
  is left out (network interfaces, store addresses, proxies, library paths,
  any value that is an address or a path no mount provides). A second model a
  flag reads, such as a draft, is kept at `/data/models/<name>`, where sites
  keep weights; a cache mount becomes an empty directory; a mount nothing
  reads is left out. A name no rule knows is kept and flagged: say what it is,
  for this export or, with **Remember**, for every export.

Every file is checked against the catalog's schemas and the rules its own
validator adds, as you go; the export can be downloaded once it passes.
Nothing is pushed: **Download** gives a zip laid out as the catalog is.
Unpack it at the root of a model-catalog checkout and open a pull request. The
provenance lines (which campaign and run measured each variant) are for the
pull request description. To run the catalog's own checks on it first:

```bash
deploy/dev/check-catalog-export.sh <export.zip> <model-catalog checkout>
```

When a plugin groups every selected run into tracks, the zip also carries
`<name>.validation.json`: each variant's track, source run and role, and a
hash of each variant file's content (formatting does not change it). Catalog
CI can use it to route new optimized variants back to the tracks they were
measured on; the catalog's files and schema are unchanged by it.

---

← [Running a campaign](running-campaigns.md) · [User guide](README.md) · Next: [Baselines](baselines.md) →
