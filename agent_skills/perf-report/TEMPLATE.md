# Report template

The reader wants one thing: **the best way to serve this model on this card, and
how much better it is than the baseline.** Everything in the report serves that.

Every report is written **twice, in English and in Chinese** — two markdown
files, same structure, same blocks — and the Chinese one is saved as a
translation of the English one.

Follow the structure below exactly. Text in `<angle brackets>` is filled in;
text in *italics* is guidance and does not appear in the report.

## Blocks, not numbers

Charts, tables and serving commands are **blocks**: a fenced block that names
what to draw. The platform draws it from the data frozen with the report, in the
report's language, with the config names and styling already right. Every block
these runs can draw is in `work/blocks.md`; paste them from there, unchanged.

````markdown
```chart
type: summary
```
````

- Never write a markdown table of results, never paste a serving command, never
  add an image. If a block exists for it, use the block.
- Prose may quote a number to make its point (the headline gain, the concurrency
  a config reached), copied from `work/comparison.json` — at most a few per
  section, and never a whole row of a table.

## Style rules

- **Bullets, not paragraphs.** Every piece of prose in the report is a short
  bullet list: one fact per bullet, one line each where it fits, no bullet
  longer than two lines. Never write a paragraph of running text.
- **Bold the number the bullet is about** — the throughput, the change in
  percent, the concurrency, the score. One or two per bullet, never a whole
  clause: `serves **17,200 tok/s per GPU**, **+19.4%** vs Baseline`.
- **Report, don't judge.** State what was configured and what was measured. No
  explanations of why a number moved, no claims about engine defaults or which
  flags "really" matter, no advice. If the data does not say it, the report does
  not say it.
- **Public wording only.** Never mention run ids, machine names,
  node or machine names, GPU indices, pods, Kubernetes, docker, ports, config
  hashes, API field names (`comparable`, `best.overall`, `score_card_norm`, …),
  campaigns, or how the dataset was collected. Configs are **Baseline**
  and **Optimized** (or the names the person gives, passed as `--labels`).
- **Throughput means at the best concurrency within SLO** — the
  highest-throughput level that met the SLO (`best_level` in the data).
- Round for reading: throughput to 3 significant figures, percentages to one
  decimal, latency in ms below 10 s and seconds above.
- If the runs are not comparable, the conclusion's first sentence says so and
  why, in plain words.

## Glossary — use exactly these terms

| English | 中文 |
|---|---|
| Baseline | 基线 |
| Optimized | 优化配置 |
| SLO | SLO |
| throughput per GPU | 单卡吞吐 |
| best concurrency within SLO | SLO 内最佳并发 |
| concurrency | 并发 |
| input N / output M (a sweep) | 输入 N / 输出 M |
| Agentic dataset (concurrency N) | Agentic 数据集(并发 N) |
| time to first token (TTFT) | 首 token 延迟(TTFT) |
| time per output token (TPOT) | 每输出 token 耗时(TPOT) |
| serving command | 启动命令 |
| quality | 质量 |
| config diff | 配置差异 |

---

````markdown
# <Optimizing <model> on <card> | 在 <card> 上优化 <model>>

## <Conclusion | 结论>

```chart
type: summary
```

<Bullets, in this order — one line each, and nothing else:
- one bullet per scenario: its name, the Optimized config's throughput per GPU
  at the best concurrency within SLO, and the change against Baseline in percent
- one bullet for quality: the benchmark, both scores, and the change
- the hardware the numbers are per: <N>× <card>
No why.>

**<Serving command | 启动命令>**

```command
config: 1
```

```table
type: summary
```

```table
type: quality
```

*(the quality table carries its own note on what quality is for; do not repeat it)*

**<Config diff | 配置差异>**

```table
type: diff
attempt: 1
```

**<Worth knowing | 注意事项>** *(omit the heading and the list if there is nothing)*

<Up to three bullets, only if the data shows them: a measured metric that got
worse (with the change), a scenario that did not improve, a failed attempt.
Facts only.>

## <Experimental setup | 实验设置>

```table
type: setup
```

```table
type: scenarios
```

*(the scenarios table carries its own note on how a sweep picks its level and
that the agentic dataset has a fixed concurrency; do not repeat it)*

## <Results | 结果>

### <scenario name> *(one section per sweep, in the order of blocks.md)*

```chart
type: sweep
scenario: <scenario key from blocks.md>
```

<Up to 3 bullets reading the two figures it draws (throughput: total and output;
latency: TTFT and TPOT, p50 by default with a p90/p99 switch):
- Baseline: its best concurrency within SLO, and the throughput and TTFT there
- Optimized: the same, with the change in percent
- at most one more, only if a figure shows something the first two miss
Nothing the figures do not show.>

### <Agentic dataset (concurrency N) | Agentic 数据集(并发 N)> *(only if blocks.md has it)*

```chart
type: agentic
scenario: <scenario key from blocks.md>
```

<Up to 3 bullets reading the chart, as changes in percent: input throughput
(uncached / cached), output throughput, and TTFT p50/p90/p99.>

### <Optimized N> — <did not finish | 未完成> *(only for a failed attempt)*

<Two bullets: what it changed, and the failure as the data reports it.>

## <Appendix: Baseline serving command | 附录:基线启动命令>

```command
config: baseline
```
````

*With several attempts: one `command`, `summary` and `diff` per attempt where the
reader needs it, in the order the person gave them; the conclusion names the best.*
