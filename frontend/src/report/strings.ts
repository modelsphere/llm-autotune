/** Every word a report's tables and charts show, per language. Prose is the
 *  agent's; these are the renderer's, so the same term reads the same way in
 *  every report. Keep in step with the glossary in the perf-report skill's
 *  TEMPLATE.md. */

export type Lang = 'en' | 'zh'

const en = {
  baseline: 'Baseline',
  optimized: 'Optimized',
  optimizedN: 'Optimized {n}',
  sweep: 'Input {inp} / output {out}',
  agentic: 'Agentic dataset (concurrency {c})',
  concurrency: 'concurrency',
  atConcurrency: '@ concurrency {c}',
  tpsPerGpu: 'tok/s/GPU',
  scenario: 'Scenario',
  benchmark: 'Benchmark',
  setting: 'Setting',
  workload: 'Workload',
  method: 'How it is measured',
  item: 'Item',
  value: 'Value',
  model: 'Model',
  precision: 'Precision',
  hardware: 'Hardware',
  hardwareValue: '{n}× {card} per server',
  image: 'Image',
  slo: 'SLO',
  sloValue: 'TTFT {pct} ≤ {ms} ms, ≥ {tps} tok/s per request',
  summaryTitle: 'Total throughput per GPU at the best concurrency within SLO',
  totalPerGpu: 'total tokens/s per GPU',
  ttftAxis: 'TTFT {pct} (ms)',
  sloLine: 'SLO: TTFT {pct} ≤ {ms} ms',
  bestMarks: 'dashed: best concurrency within SLO',
  throughputPanel: 'Throughput per GPU',
  sweepThroughput: '{name}: throughput',
  sweepLatency: '{name}: latency',
  totalPanel: 'Total tokens/s per GPU',
  outputPanel: 'Output tokens/s per GPU',
  tpotAxis: 'TPOT {pct} (ms)',
  percentile: 'Percentile',
  ttftPanel: 'Time to first token (ms)',
  inputUncached: 'input (uncached)',
  inputCached: 'input (cached)',
  output: 'output',
  workloadSweep: 'random prompts, {inp} input / {out} output tokens',
  methodSweep: 'concurrency search (up to {max}); best concurrency within SLO',
  workloadAgentic: '{n} agentic requests',
  methodAgentic: 'replayed at concurrency {c}',
  quality: 'Quality',
  accuracy: 'accuracy on a fixed question set',
  qualityNote: 'Quality is measured so a faster config is not a worse one: every config answers the '
    + 'same question set, and the scores are compared to the baseline\u2019s.',
  sweepNote: 'A sweep raises concurrency and reports the level with the highest throughput that '
    + 'still meets the SLO, so each config is read at its own best concurrency.',
  agenticNote: 'The agentic dataset runs at one fixed concurrency, so the SLO does not pick its level.',
  copy: 'Copy',
  copied: 'Copied',
  unknownBlock: 'This block could not be drawn: {why}',
  noData: 'not measured',
  identical: 'identical launch settings',
}

type Strings = typeof en

const zh: Strings = {
  baseline: '基线',
  optimized: '优化配置',
  optimizedN: '优化配置 {n}',
  sweep: '输入 {inp} / 输出 {out}',
  agentic: 'Agentic 数据集(并发 {c})',
  concurrency: '并发',
  atConcurrency: '@ 并发 {c}',
  tpsPerGpu: 'tok/s/GPU',
  scenario: '场景',
  benchmark: '评测',
  setting: '参数',
  workload: '负载',
  method: '测法',
  item: '项目',
  value: '值',
  model: '模型',
  precision: '精度',
  hardware: '硬件',
  hardwareValue: '每个服务 {n}× {card}',
  image: '镜像',
  slo: 'SLO',
  sloValue: 'TTFT {pct} ≤ {ms} ms,每请求 ≥ {tps} tok/s',
  summaryTitle: 'SLO 内最佳并发下的单卡总吞吐',
  totalPerGpu: '单卡总吞吐(tokens/s)',
  ttftAxis: 'TTFT {pct}(ms)',
  sloLine: 'SLO:TTFT {pct} ≤ {ms} ms',
  bestMarks: '虚线:SLO 内最佳并发',
  throughputPanel: '单卡吞吐',
  sweepThroughput: '{name}:吞吐',
  sweepLatency: '{name}:延迟',
  totalPanel: '单卡总吞吐(tokens/s)',
  outputPanel: '单卡输出吞吐(tokens/s)',
  tpotAxis: 'TPOT {pct}(ms)',
  percentile: '分位',
  ttftPanel: '首 token 延迟(ms)',
  inputUncached: '输入(未命中缓存)',
  inputCached: '输入(命中缓存)',
  output: '输出',
  workloadSweep: '随机 prompt,输入 {inp} / 输出 {out} tokens',
  methodSweep: '并发自动搜索(上限 {max}),取 SLO 内最佳并发',
  workloadAgentic: '{n} 条 agentic 请求',
  methodAgentic: '并发 {c} 回放',
  quality: '质量',
  accuracy: '固定题集上的准确率',
  qualityNote: '质量测试用于确认更快的配置不会变差:每个配置回答同一套题目,分数与基线对比。',
  sweepNote: '并发扫描会逐步提高并发,取仍满足 SLO 的最高吞吐档位,因此每个配置都在各自的最佳并发下比较。',
  agenticNote: 'Agentic 数据集使用固定并发,其档位不由 SLO 选出。',
  copy: '复制',
  copied: '已复制',
  unknownBlock: '此区块无法绘制:{why}',
  noData: '未测',
  identical: '启动参数相同',
}

const TABLE: Record<Lang, Strings> = { en, zh }

export type StringKey = keyof Strings

export function tr(lang: Lang, key: StringKey, vars: Record<string, string | number> = {}): string {
  const text = (TABLE[lang] ?? en)[key]
  return text.replace(/\{(\w+)\}/g, (_m, name) => (name in vars ? String(vars[name]) : `{${name}}`))
}
