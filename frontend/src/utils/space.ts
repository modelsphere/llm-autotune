/** Reading a search space the way the planner does.
 *
 *  The campaign's own declaration is the source of truth for what varies —
 *  inferring it by diffing candidate configs disagrees the moment two points
 *  produce the same value, or a single-point axis is swept deliberately.
 *
 *  This file exists because that reading was written twice: the campaign
 *  detail page had its own copy that read `grid` and `tied` but never `range`,
 *  so a range-based sweep rendered twenty distinct configs as two repeated
 *  lines. One reader, used by everything that shows a space.
 */

export interface SpaceShape {
  name?: string
  base?: Record<string, unknown>
  grid?: Record<string, unknown[]>
  /** Groups swept together rather than crossed: zipped, not multiplied. */
  tied?: Record<string, unknown[]>[]
  range?: Record<string, { min?: unknown; max?: unknown; step?: unknown }>
  /** {param: {gating_param: [values]}} — dropped from candidates that do not
   *  satisfy the gate, so those configs deploy identically and are one point. */
  conditions?: Record<string, Record<string, unknown[]>>
}

export type AxisKind = 'list' | 'tied' | 'range'

export interface Axis {
  /** What to show: a tied group is one axis called "a + b". */
  label: string
  /** The parameters this axis moves. */
  keys: string[]
  kind: AxisKind
  /** The points, spelled out. Long intervals are cut — see `hidden`. */
  values: string[]
  hidden: number
  /** How many points the axis really has, cut or not. */
  count: number
  /** Set when the parameter only applies for certain values of another. */
  gate?: { on: string; values: string[] }
  /** For a range: the interval as written, which is what was authored. */
  interval?: string
}

const MAX_SHOWN = 8

function expandRange(spec: { min?: unknown; max?: unknown; step?: unknown }): {
  values: string[]
  usable: boolean
} {
  const min = Number(spec?.min)
  const max = Number(spec?.max)
  const step = Number(spec?.step)
  const usable = [min, max, step].every(Number.isFinite) && step > 0 && max >= min
  if (!usable) return { values: [], usable: false }
  const count = Math.floor((max - min) / step + 1e-9) + 1
  // Floating steps accumulate error; round to the step's own precision so a
  // 0.05 step does not render 0.7000000000000001.
  const decimals = (String(spec?.step).split('.')[1] ?? '').length
  return {
    values: Array.from({ length: count }, (_, i) =>
      String(Number((min + i * step).toFixed(decimals))),
    ),
    usable: true,
  }
}

function cut(values: string[]): { values: string[]; hidden: number; count: number } {
  if (values.length <= MAX_SHOWN) {
    return { values, hidden: 0, count: values.length }
  }
  // Keep both ends: the bounds of an interval are what a reader checks.
  return {
    values: [...values.slice(0, MAX_SHOWN - 1), values[values.length - 1]],
    hidden: values.length - MAX_SHOWN,
    count: values.length,
  }
}

/** One entry per axis of the sweep, in the order a reader scans them:
 *  listed values, then intervals, then tied groups. */
export function axesOf(space: SpaceShape | null | undefined): Axis[] {
  if (!space) return []
  const axes: Axis[] = []

  for (const [key, raw] of Object.entries(space.grid ?? {})) {
    const values = (Array.isArray(raw) ? raw : [raw]).map(String)
    axes.push({ label: key, keys: [key], kind: 'list', ...cut(values) })
  }

  for (const [key, spec] of Object.entries(space.range ?? {})) {
    const { values, usable } = expandRange(spec)
    const interval = `${spec?.min} … ${spec?.max} step ${spec?.step}`
    axes.push(
      usable
        ? { label: key, keys: [key], kind: 'range', interval, ...cut(values) }
        : { label: key, keys: [key], kind: 'range', interval,
            values: [], hidden: 0, count: 0 },
    )
  }

  for (const group of space.tied ?? []) {
    const keys = Object.keys(group)
    if (!keys.length) continue
    // A ragged group is zipped to its shortest column; the tail has no partner.
    const rows = Math.min(...keys.map((k) => group[k].length))
    const values = Array.from({ length: rows }, (_, i) =>
      keys.map((k) => String(group[k][i])).join(' / '),
    )
    axes.push({ label: keys.join(' + '), keys, kind: 'tied', ...cut(values) })
  }

  for (const [key, clause] of Object.entries(space.conditions ?? {})) {
    const [on, allowed] = Object.entries(clause ?? {})[0] ?? []
    if (!on) continue
    const axis = axes.find((a) => a.keys.includes(key))
    if (axis) axis.gate = { on, values: (allowed ?? []).map(String) }
  }

  return axes
}

/** Every parameter that varies — the columns worth showing per candidate. */
export function sweptKeysOf(space: SpaceShape | null | undefined): string[] {
  return axesOf(space).flatMap((axis) => axis.keys)
}

/** Upper bound on the candidate count from the axes alone.
 *
 *  Deliberately labelled "at most": conditions prune parameters, which merges
 *  configs that deploy identically, and static validation rejects others. The
 *  authoritative count comes from the backend that expands the space. */
export function crossProduct(space: SpaceShape | null | undefined): number {
  return axesOf(space).reduce((total, axis) => total * Math.max(1, axis.count), 1)
}
