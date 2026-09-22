/** A campaign as YAML, both directions.
 *
 *  Building a campaign through six wizard steps is right for the first one and
 *  tedious for the tenth, especially when the tenth differs from the ninth by
 *  one grid value. This is the same thing as a file: export one, edit it, bring
 *  it back.
 *
 *  The field names are the API's own, deliberately. A prettier nested shape
 *  (`schedule: {start, end}`) would need a mapping in each direction, and a
 *  mapping that exists twice is one that eventually disagrees with itself —
 *  which here means a campaign that runs against a different benchmark than the
 *  file says. What you export is what the API is posted.
 */
import { fromYaml, toYaml } from './yaml'

/** Everything a campaign needs to be recreated — and nothing the server owns.
 *  `status`, `window_start/end` and `override_until` are results of running,
 *  not inputs; carrying them would let a file resurrect a stale window. */
export const PORTABLE_KEYS = [
  'name',
  'engine',
  'image',
  'model_path',
  'served_model_name',
  'machine_names',
  'service_port',
  'share_machine',
  'max_run_minutes',
  'planner',
  'run_baseline_canary',
  'extra_env',
  'extra_volumes',
  'search_space',
  'objective',
  'benchmark_slug',
  'confirm_top_k',
  'confirm_repeats',
  'verify_benchmark_slug',
  'verify_top_k',
  'verify_objective',
  'verify_max_run_minutes',
  // The profile and the policy travel; the pin does not. A pinned build id is
  // something this campaign holds, not something a copy of it should claim.
  'dataset_profile',
  'dataset_policy',
  'daily_start',
  'daily_end',
  'schedule_timezone',
  'schedule_until',
] as const

const HEADER = `# LLM Autotune campaign.
#
# Import this on the New campaign page to fill the form in, or post it to
# POST /api/campaigns as JSON. Field names are the API's own.
#
# search_space.grid is crossed; range is an interval; conditions gate a
# parameter on another's value; tied moves parameters together.
`

export function campaignToYaml(campaign: Record<string, unknown>): string {
  const out: Record<string, unknown> = {}
  for (const key of PORTABLE_KEYS) {
    const value = campaign[key]
    // Skip what was never set, so a hand-edited file is not buried in empty
    // strings and nulls that mean "default".
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value) && !value.length) continue
    if (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length) {
      continue
    }
    out[key] = value
  }
  return HEADER + toYaml(out)
}

export interface ImportedCampaign {
  fields: Record<string, unknown>
  search_space: Record<string, unknown> | null
  objective: Record<string, unknown> | null
  verify_objective: Record<string, unknown> | null
  /** Keys in the file that this version does not know — surfaced rather than
   *  dropped, because a silently ignored `verify_top_k` is a campaign that
   *  quietly never verifies. */
  unknown: string[]
}

export function campaignFromYaml(text: string): ImportedCampaign {
  const parsed = fromYaml<Record<string, unknown>>(text, 'Campaign YAML')
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Campaign YAML must be a mapping of fields')
  }
  const known = new Set<string>(PORTABLE_KEYS)
  const fields: Record<string, unknown> = {}
  const unknown: string[] = []
  for (const [key, value] of Object.entries(parsed)) {
    if (known.has(key)) fields[key] = value
    else unknown.push(key)
  }
  const take = (key: string) => {
    const value = fields[key]
    delete fields[key]
    return value && typeof value === 'object' ? (value as Record<string, unknown>) : null
  }
  return {
    search_space: take('search_space'),
    objective: take('objective'),
    verify_objective: take('verify_objective'),
    fields,
    unknown,
  }
}
