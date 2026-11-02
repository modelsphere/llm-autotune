[User guide](README.md) › **Baselines**

# Baselines

A **baseline** is the config production runs today for one model, on one
engine and one GPU type: the image, the weights path, the port, the engine
settings, the environment and the mounts. Baselines are under
**Library ▸ Baselines**.

Recording a baseline is optional, but it gives a campaign's numbers a
reference: "+12% over production, measured the same way in the same window"
instead of an absolute throughput figure.

## What a baseline does

When a campaign tunes a model that has a baseline (same **served model
name** and **engine**, on the same **card type**, or any card if the baseline
specifies none):

- **The campaign measures production's config too.** It starts the
  baseline's config on one of its machines as an additional run and
  benchmarks it like the candidates. On the leaderboard it is the row tagged
  **baseline**.
- **Every result is compared with it**, in the **vs baseline** column and in
  the report.
- **The Check step compares the campaign with it** (**Matches production**),
  and the **Configuration** tab lists flags production sets that the campaign
  does not (**Difference from production**).
- **It can draft a campaign**: **Tune from this**.

![A campaign with a baseline: production's row, and every config against it](images/baseline-leaderboard.png)

## Recording one

Press **New baseline**. The quickest way is to paste production's launch
command into **Launch command** (the whole `docker run …` line or only the
serve command) and press **Parse into fields**.

![A baseline parsed from a docker run line](images/baseline-editor.png)

The parse fills in:

- **Image**, **Model path** and **Port**;
- **Engine args**: every engine flag, as JSON;
- **Env and volumes**: environment variables and mounts, as YAML.

Flags that only place the engine (the model path inside the container, the
host, the served name) are dropped, because every run sets them itself.

Then check the top row:

- **Served model**: the name the model is served under. Campaigns are matched
  to the baseline by this name, so use the same name in campaigns (a
  campaign's served name defaults to the last component of its model path).
- **Engine**: `sglang` or `vllm`.
- **Card type**: the GPU model, such as `H100`. A baseline is only compared with
  runs on the same card; leave it empty to match any card.

Press **Save**.

> **Port:** an engine port between 30000 and 32767 triggers a warning: on
> Kubernetes nodes that range is reserved for NodePorts, and an engine there
> is unreachable. Campaigns use their own ports, from 28200 by default.

## The list

![The Baselines list](images/baselines-list.png)

Each row shows the served model, engine, card type, GPU count and the main
engine settings. Expand a row for the image, the weights path and the
environment. **Source** shows where it came from (*manual* for one entered on
this page). **Edit** and **Delete** change or remove it; campaigns that
already measured it keep their results.

## Tune from this

**Tune from this** opens New campaign with a draft based on the baseline:

- its image, model path and engine settings, with the settings as the fixed
  part of the search space;
- `tp` swept around production's value, as wide as the machines allow;
- the default benchmark, objective (**Throughput per GPU**) and window.

The wizard opens on the **Check** step, with a note listing the source of each
value:

![A campaign drafted from a baseline](images/draft-check.png)

Review the steps (especially the benchmark and the search space), then
**Create campaign**. **Start from ▸ A baseline** on New campaign does the same,
and can also draft from a model name alone.

After deploying a better config, **Edit** the baseline to match, so the next
campaign measures against the new production config.

---

← [Reading results](results.md) · [User guide](README.md) · Next: [Runs and failures](runs.md) →
