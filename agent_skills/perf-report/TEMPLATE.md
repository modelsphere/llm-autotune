# Report template

The reader wants one thing: **the best way to serve this model on this card, and
how much better it is than the baseline.** Everything in the report serves that.

It reads like a lab report: numbered sections, running prose with the numbers in
bold, and numbered captions on its figures and results tables.

Every report is written **twice, in English and in Chinese** — two markdown
files, same structure, same blocks — and the Chinese one is saved as a
translation of the English one.

Follow the structure below exactly. Text in `<angle brackets>` is filled in;
text in *italics* is guidance and does not appear in the report.

## Three ideas the whole report rests on

Read these before anything else; every number in the report depends on them.

**1. The only throughput is normalized throughput.** Configs may
serve the model on different numbers of GPUs (a 2-GPU instance against an
8-GPU one). To compare them, every throughput in the report is normalized to 8
GPUs: a config served on N GPUs is scaled by 8/N. It is called **normalized
throughput** / **归一化吞吐** (add "(8 GPUs)" / "(8 卡)" the first time it
appears, nowhere else) — never "total serving throughput", never
"per GPU", never a config's raw measured rate. The platform has already done
the normalizing: quote `best_level.total_tps_per_machine` (and
`output_tps_per_machine`) and the `pct` of its delta. Never quote a `*_per_gpu`
field or a throughput from `levels[]` (those are raw), and never multiply or
divide a throughput yourself. When the configs use different GPU counts, say so
once in 1.2 (e.g. "from 2 to 8 GPUs per instance") and nowhere else — the
normalization already makes their throughput comparable.

**2. The SLO decides which concurrency counts.** A sweep raises concurrency
step by step. A level *meets the SLO* only when both hold: TTFT at the SLO's
percentile is at or under its limit, **and** per-request generation speed is at
or above its minimum (`benchmark.platform.slo`; `meets_slo` on each level).
Latency and per-request speed are limits, not goals.

**3. Each config is read at its own best concurrency within SLO** — among the
levels that met the SLO, the one with the highest normalized throughput
(`best_level`). It is not always the highest passing concurrency, and the two
configs usually land on different ones. What bounds it is the lowest measured
concurrency that missed the SLO (the search may measure a few levels past it;
quote the lowest miss). If every measured level met the SLO, the search ran to
its concurrency limit, and the report says the highest measured concurrency
still met the SLO. An agentic (replay) scenario runs at one fixed concurrency, so the
SLO does not choose its level.

## Blocks, not numbers

Charts, tables and serving commands are **blocks**: a fenced block that names
what to draw. The platform draws it from the data frozen with the report, in the
report's language, with the config and scenario names already right. Every block
these runs can draw is in `work/blocks.md`; paste them from there, unchanged.

````markdown
```chart
type: summary
```
````

- Never write a markdown table of results, never paste a serving command, never
  add an image. If a block exists for it, use the block.
- Prose quotes the numbers it makes a point about — the headline gain, a
  concurrency, a latency — copied from `work/comparison.json`. Never restate a
  whole table in prose.

## What the platform writes — never write these

Text that must read the same in every report is drawn with the blocks, in both
languages. Do not write it, paraphrase it or restate it:

- **Captions.** Every chart gets a numbered caption under it ("Figure 2. …")
  and the `summary` and `quality` tables one over them ("Table 1. …"),
  numbered in order. `check` refuses a caption you write yourself.
- **Definitions.** What TTFT, per-request generation speed and normalized
  throughput mean, the SLO limits, and how the best concurrency is chosen
  (`table type: slo`).
- **Notes under tables:** what "—" means in the diff, that sweeps use random
  prompts, what the quality run is for, and that throughput at each config's
  own best concurrency is a serving capacity, not a per-request speedup.

What is yours: the title, the section headings exactly as the structure gives
them, the scenario names and descriptions, and the paragraphs that read this
report's numbers.

## Naming the scenarios

A scenario is called "Input 50k / output 1.5k" unless the report gives it a
name. Give it one: a name a reader recognises, and one sentence saying what it
stands for. Write them to `work/scenarios.en.json` and `work/scenarios.zh.json`
and pass the file as `--scenarios` on the matching save, so every chart, table
and heading calls the scenario what the prose calls it.

```json
{
  "perf_guidellm_sweep": {
    "name": "Agent long-context",
    "description": "One agent call carrying a long task context, interaction history and tool output, then the analysis or action it generates."
  },
  "perf_guidellm_sweep#2": {
    "name": "Document QA",
    "description": "Question answering over a long document or several retrieved passages: question and material in, answer or summary out."
  }
}
```

The Chinese file uses the Chinese names, ending in 场景 (`scenarios.zh.json`):

```json
{
  "perf_guidellm_sweep": {"name": "Agent 长上下文场景", "description": "模拟 Agent 在单次调用中携带较长的任务上下文、历史交互和工具返回内容,并生成后续分析或行动内容。"},
  "perf_guidellm_sweep#2": {"name": "长文档问答场景", "description": "模拟基于一篇长文档或多段检索材料进行问答,输入为问题与参考材料,输出为回答或摘要。"}
}
```

Name it after the workload the input/output shape stands for (long context in
and a short answer out is an agent call; a few thousand tokens in and an answer
out is document QA; a replay is the agentic dataset). If the person gave the
scenarios names, use theirs. The description says what the shape simulates and
nothing about the result.

## Style rules

- **Prose, not bullets.** Every section is one short paragraph (two at most),
  running text. No bullet lists anywhere in the report.
- **Bold the numbers the paragraph is about** — the throughput, the change in
  percent, the concurrency, the score: `normalized throughput rises from
  **65,000 → 93,700 tok/s (+44.2%)**`. Bold nothing else.
- **Refer to figures and tables by number** when the prose needs to
  ("Figure 2", "Table 1" / "图 2"、"表 1"): charts and the `summary` and
  `quality` tables are numbered in the order they appear, a `sweep` block
  counting as one figure.
- **Write a change as `A → B`**, the arrow in both languages, with the percent
  after it: `**4,811 → 7,660 ms (+59.2%)**`.
- **Report, don't judge.** State what was configured and what was measured. No
  explanations of why a number moved, no claims about engine defaults or which
  flags "really" matter, no advice. If the data does not say it, the report does
  not say it.
- **Public wording only.** Never mention run ids, machine names,
  node or machine names, GPU indices, pods, Kubernetes, docker, ports, config
  hashes, API field names (`comparable`, `best.overall`, `score_card_norm`, …),
  campaigns, or how the dataset was collected. Configs are **Baseline**
  and **Optimized** (or the names the person gives, passed as `--labels`).
- **Throughput in prose is normalized throughput at the best concurrency
  within SLO** (`best_level.total_tps_per_machine`, ideas 1 and 3), nothing
  else.
- Round for reading: throughput to 3 significant figures, per-request
  generation speed to one decimal, percentages to one decimal (half up: 119.85
  is 119.9), latency in whole ms below 10 s and seconds (one decimal) above,
  concurrencies as they are.
- **Quality reads the way its table does**: a 0–1 score is a percentage with
  two decimals, and its change is given twice — in percentage points and in
  percent: `**69.19% → 66.16%**, **−3.03 percentage points (−4.4%)**`
  (中文:`**−3.03 个百分点(−4.4%)**`).
- **Name the quality benchmark** (GPQA Diamond, …): it is public, unlike run
  or machine names.
- If the runs are not comparable, the conclusion's first sentence says so and
  why, in plain words.

## Glossary — use exactly these terms

| English | 中文 |
|---|---|
| Baseline | 基线 |
| Optimized | 优化配置 |
| SLO | SLO |
| normalized throughput | 归一化吞吐 |
| normalized output throughput | 归一化输出吞吐 |
| per-request generation speed (OTPS / request) | 单请求生成速度(OTPS / request) |
| best concurrency within SLO | SLO 内最佳并发 |
| concurrency | 并发 |
| time to first token (TTFT) | 首 token 延迟(TTFT) |
| time per output token (TPOT) | 每输出 token 耗时(TPOT) |
| Agentic dataset (concurrency N) | Agentic 数据集(并发 N) |
| serving command | 启动命令 |
| answer quality | 回答质量 |
| percentage points | 个百分点 |
| Agent long-context (scenario) | Agent 长上下文场景 |
| Document QA (scenario) | 长文档问答场景 |
| Figure N / Table N | 图 N / 表 N |

---

````markdown
# <Optimizing <model> Inference on <card>: Experimental Evaluation
  | <model> 在 <card> 上的推理优化:实验评估>

## <Conclusion | 结论>

<One paragraph: what was compared, on what hardware, under what SLO, and the
change in normalized throughput per scenario, in bold. Nothing else.>

```chart
type: summary
```

### <Optimized configuration | 优化配置>

```command
config: 1
```

## <1. Experimental setup and methodology | 1. 实验设置与方法>

### <1.1 Experimental environment | 1.1 实验环境>

```table
type: setup
```

```table
type: slo
```

*(no prose here: the two tables and their notes say it all)*

### <1.2 Optimization variables | 1.2 优化变量>

```table
type: diff
attempt: 1
```

<One or two sentences on what the Optimized config changes, in the words of
the settings themselves.>

### <1.3 Scenario definitions | 1.3 场景定义>

```table
type: scenarios
```

## <2. Experimental results | 2. 实验结果>

### <2.1 Serving capacity under the SLO | 2.1 SLO 约束下的服务容量>

```table
type: summary
```

<One paragraph: per scenario, the best concurrency and normalized throughput
from Baseline to Optimized with the change in percent, all bold.>

### <2.2 Concurrency scaling and latency trade-offs | 2.2 并发扩展与延迟权衡>

#### <2.2.1 <scenario name>> *(one sub-section per sweep, in blocks.md order)*

```chart
type: sweep
scenario: <scenario key from blocks.md>
```

<One paragraph reading the two figures at each config's best concurrency: TTFT
and per-request generation speed with their changes, and where the SLO stops
the search — the first concurrency that misses it and the value there against
the limit, or, if none missed, that the highest measured concurrency still met
the SLO (idea 3). Nothing the figures do not show.>

#### <2.2.N <Agentic dataset (concurrency N)>> *(only if blocks.md has it)*

```chart
type: agentic
scenario: <scenario key from blocks.md>
```

<One paragraph: input throughput (uncached / cached), output throughput and
TTFT p50/p90/p99, as changes in percent.>

### <2.3 Answer quality | 2.3 回答质量>

```table
type: quality
```

<One or two sentences: the score before and after, and the change in
percentage points and in percent, bold (see the quality rounding rule).>

### <2.4 <Optimized N> — did not finish | 2.4 <优化配置 N> — 未完成>
*(only for a failed attempt)*

<Two sentences: what it changed, and the failure as the data reports it.>

## <Appendix: serving commands | 附录:启动命令>

### <Baseline | 基线>

```command
config: baseline
```
````

*With several attempts: one `command` and one `diff` per attempt, in the order
the person gave them, numbered on under 1.2; the conclusion names the best.*
