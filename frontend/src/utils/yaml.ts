/** YAML for anything a human types.
 *
 *  Search spaces and objectives are hand-edited config, and JSON is a poor
 *  format for that: no comments, quotes everywhere, and a missing comma fails
 *  with a message that does not say where. YAML also accepts JSON, so anything
 *  previously pasted still parses.
 */

import { dump, load } from 'js-yaml'

export function toYaml(value: unknown): string {
  if (value === null || value === undefined) return ''
  return dump(value, { indent: 2, lineWidth: 100, noRefs: true, sortKeys: false })
}

export class YamlError extends Error {}

/** Parse, insisting on a mapping — a bare scalar or list is never a valid
 *  search space/objective, and failing here beats a confusing 422 later. */
export function fromYaml<T = Record<string, unknown>>(text: string, what = 'value'): T {
  const trimmed = (text ?? '').trim()
  if (!trimmed) return {} as T
  let parsed: unknown
  try {
    parsed = load(trimmed)
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message.split('\n')[0] : String(error)
    throw new YamlError(`${what}: ${message}`)
  }
  if (parsed === null || parsed === undefined) return {} as T
  if (typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new YamlError(`${what} must be a mapping (key: value), not a ${typeof parsed}`)
  }
  return parsed as T
}
