/** A campaign's workload as the form holds it, and the API spec it becomes
 *  (backend app/evaluation/benchmark_spec.py). See WorkloadPicker.vue. */

export interface Workload {
  mode: 'sweep' | 'replay' | 'bfcl' | 'existing'
  bfcl: BFCLConfig
  input_tokens: number
  output_tokens: number
  /** Comma-separated, as typed. */
  concurrencies: string
  /** Level c sends c × this many requests. */
  requests_per_concurrency: number
  /** Caps a level that has not finished its requests. */
  max_seconds_per_level: number
  /** A rolling collection profile; '' with no frozen one = the example set. */
  dataset_profile: string
  /** A frozen dataset instead: a file that never changes. */
  dataset_frozen: string
  requests: number
  concurrency: number
  slug: string
}

export function defaultWorkload(mode: Workload['mode'] = 'sweep'): Workload {
  return {
    mode, bfcl: defaultBFCL(), input_tokens: 2048, output_tokens: 512, concurrencies: '1, 4, 16, 64',
    requests_per_concurrency: 20, max_seconds_per_level: 600, dataset_profile: '', dataset_frozen: '', requests: 500, concurrency: 16, slug: '',
  }
}

/** The API's BenchmarkSpec for a described workload; null for an existing one. */
export function specOf(w: Workload): Record<string, unknown> | null {
  if (w.mode === 'existing') return null
  if (w.mode === 'bfcl') return { kind: 'bfcl', bfcl: w.bfcl }
  if (w.mode === 'sweep') {
    return {
      kind: 'sweep', input_tokens: w.input_tokens, output_tokens: w.output_tokens,
      concurrencies: w.concurrencies.split(/[\s,]+/).map(Number).filter((n) => n > 0),
      requests_per_concurrency: w.requests_per_concurrency,
      max_seconds_per_level: w.max_seconds_per_level,
    }
  }
  return {
    kind: 'replay', requests: w.requests, concurrency: w.concurrency,
    ...(w.dataset_frozen ? { dataset_frozen: w.dataset_frozen }
      : { dataset_profile: w.dataset_profile }),
  }
}

export interface BenchmarkChoice {
  slug: string
  name: string
  modules: string[]
  replays_profile: string
}

export interface BFCLConfig {
  task_set: string
  cases_per_category: number
  concurrency: number
  concurrency_grid?: number[] | null
  max_total_seconds?: number | null
  max_seconds: number
  request_timeout_seconds: number
  request_retries: number
  probe_endpoint: boolean
  required_metrics: string[]
  min_ttft_coverage: number
  min_usage_coverage: number
  min_cache_usage_coverage: number
}

export function defaultBFCL(): BFCLConfig {
  return {
    task_set: 'bfcl-v4-tool-use-v1', cases_per_category: 20, concurrency: 2,
    max_seconds: 1800, request_timeout_seconds: 120, request_retries: 0,
    probe_endpoint: false, required_metrics: ['accuracy', 'passed_cases_per_hour', 'ttft_p90_ms'],
    min_ttft_coverage: 1, min_usage_coverage: 0, min_cache_usage_coverage: 0,
  }
}
