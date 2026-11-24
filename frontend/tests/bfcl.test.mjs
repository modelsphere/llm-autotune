import assert from 'node:assert/strict'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

// Bundle the real renderer and spec builder; no DOM or served backend is needed.
const { outputFiles } = await build({
  stdin: {
    contents: `export * from './src/utils/workload.ts';
      export { bfclTable, qualityTable, summaryTable } from './src/report/tables.ts';
      export { bfclChart } from './src/report/charts.ts';`,
    resolveDir: fileURLToPath(new URL('..', import.meta.url)),
  },
  bundle: true, write: false, platform: 'node', format: 'esm',
})
const { defaultWorkload, specOf, bfclTable, qualityTable, summaryTable, bfclChart } =
  await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`)

function comparison() {
  const scenario = {
    key: 'bfcl_v4', kind: 'bfcl', label: 'BFCL <tool-use>', levels: [],
    summary: { case_concurrency: 2 },
    metrics: { accuracy: 0.75, passed_cases_per_hour: 180, ttft_p90_ms: 250,
      ttft_coverage: 1, input_usage_coverage: 1, output_usage_coverage: 1,
      cache_usage_coverage: 0.8 },
    best_level: { total_tps_per_gpu: 60, case_concurrency: 2 },
  }
  const run = (id) => ({ run_id: id, label: `Run ${id}`, launch: { cards: 2 },
    results: { status: 'measured', scenarios: [structuredClone(scenario)],
      headline: { quality: { 'bfcl_v4.accuracy': 0.75 } }, verdicts: [] } })
  return { benchmark: { modules: [{ key: 'bfcl_v4', params: {
    task_set: 'bfcl-v4-tool-use-v1', cases_per_category: 20 } }], platform: {} },
    baseline: run(1), attempts: [{ ...run(2), position: 1,
      deltas: { scenarios: [], quality: {} } }] }
}

test('BFCL workload sends native parameters and existing modes remain compatible', () => {
  const w = defaultWorkload('bfcl')
  w.bfcl.concurrency = 4
  assert.deepEqual(specOf(w), { kind: 'bfcl', bfcl: w.bfcl })
  assert.equal(w.bfcl.cases_per_category, 20)
  assert.equal(specOf(defaultWorkload('replay')).kind, 'replay')
  assert.equal(specOf(defaultWorkload('sweep')).kind, 'sweep')
  assert.equal(specOf(defaultWorkload('existing')), null)
})

test('BFCL tables separate case throughput and coverage from quality percentages', () => {
  const doc = comparison()
  const labels = new Map([[1, 'Base'], [2, 'Try']])
  const detail = bfclTable(doc, 'en', labels)
  assert.match(detail, /75\.00%/)
  assert.match(detail, /180/)
  assert.match(detail, /Case concurrency/)
  assert.match(detail, /BFCL &lt;tool-use&gt;/)
  const quality = qualityTable(doc, 'en', labels)
  assert.match(quality, /accuracy/)
  assert.doesNotMatch(quality, /coverage|passed_cases_per_hour/)
  const summary = summaryTable(doc, 'en', labels)
  assert.match(summary, /Passed cases\/hour/)
  assert.doesNotMatch(summary, /Best concurrency within SLO/)
})

test('BFCL chart uses accuracy percentages, cases per hour and null for missing data', () => {
  const doc = comparison()
  delete doc.attempts[0].results.scenarios[0].metrics.accuracy
  const chart = bfclChart(doc, 'en', new Map(), 'bfcl_v4')
  assert.equal(chart.series[0].data[0].value, 75)
  assert.equal(chart.series[0].data[1].value, null)
  assert.equal(chart.series[1].data[0].value, 180)
})

test('BFCL scans plot every concurrency and render every point in the report table', () => {
  const doc = comparison()
  for (const run of [doc.baseline, ...doc.attempts]) {
    run.results.scenarios[0].levels = [1, 3, 5, 6, 8].map((c) => ({
      concurrency: c, metrics: { accuracy: 0.9, total_tpm: c * 1000,
        passed_cases_per_hour: c * 20, ttft_p90_ms: c * 10, valid: 1 },
    }))
  }
  const chart = bfclChart(doc, 'en', new Map(), 'bfcl_v4')
  assert.equal(chart.series[0].type, 'line')
  assert.deepEqual(chart.series[0].data, [[1, 1000], [3, 3000], [5, 5000], [6, 6000], [8, 8000]])
  assert.equal(chart.series.length, 4)
  const table = bfclTable(doc, 'en', new Map())
  assert.match(table, /8,000/)
  assert.match(table, /TPM/)
  assert.equal((table.match(/90\.00%/g) ?? []).length, 10)
})


test('partial scans show counts and point failures; cancellation stays explicit', () => {
  const doc = comparison()
  const scenario = doc.baseline.results.scenarios[0]
  scenario.summary = { case_concurrency: 1, sweep_complete: 0,
    sweep_points_completed: 1, sweep_points_planned: 3 }
  scenario.levels = [
    { concurrency: 1, metrics: { accuracy: 0.9, total_tpm: 1000, valid: 1, eligible: 1 } },
    { concurrency: 3, metrics: { valid: 0, eligible: 0 }, error: 'Point timeout <3>' },
  ]
  const table = bfclTable(doc, 'en', new Map())
  assert.match(table, /Partial.*1.*3/)
  assert.match(table, /Point timeout &lt;3&gt;/)
  scenario.summary.sweep_canceled = 1
  assert.match(bfclTable(doc, 'en', new Map()), /Canceled/)
})
