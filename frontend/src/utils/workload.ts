/** A campaign's workload as the form holds it, and the API spec it becomes
 *  (backend app/evaluation/benchmark_spec.py). See WorkloadPicker.vue. */

export interface Workload {
  mode: 'sweep' | 'replay' | 'existing'
  input_tokens: number
  output_tokens: number
  /** Comma-separated, as typed. */
  concurrencies: string
  /** Level c sends c × this many requests. */
  requests_per_concurrency: number
  /** Caps a level that has not finished its requests. */
  max_seconds_per_level: number
  /** '' = LLMBench's built-in example set. */
  dataset_profile: string
  requests: number
  concurrency: number
  slug: string
}

export function defaultWorkload(mode: Workload['mode'] = 'sweep'): Workload {
  return {
    mode, input_tokens: 2048, output_tokens: 512, concurrencies: '1, 4, 16, 64',
    requests_per_concurrency: 20, max_seconds_per_level: 600, dataset_profile: '', requests: 500, concurrency: 16, slug: '',
  }
}

/** The API's BenchmarkSpec for a described workload; null for an existing one. */
export function specOf(w: Workload): Record<string, unknown> | null {
  if (w.mode === 'existing') return null
  if (w.mode === 'sweep') {
    return {
      kind: 'sweep', input_tokens: w.input_tokens, output_tokens: w.output_tokens,
      concurrencies: w.concurrencies.split(/[\s,]+/).map(Number).filter((n) => n > 0),
      requests_per_concurrency: w.requests_per_concurrency,
      max_seconds_per_level: w.max_seconds_per_level,
    }
  }
  return {
    kind: 'replay', dataset_profile: w.dataset_profile, requests: w.requests,
    concurrency: w.concurrency,
  }
}

export interface BenchmarkChoice {
  slug: string
  name: string
  modules: string[]
  replays_profile: string
}
