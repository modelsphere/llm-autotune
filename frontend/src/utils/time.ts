/** Timestamps people can read at a glance.
 *
 *  Raw ISO ("2026-07-30T14:21:37.756158Z") makes you do arithmetic to answer
 *  the only question you actually have: how long ago, and how long did it take.
 */

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

function parse(value: string | null | undefined): Date | null {
  if (!value) return null
  // Postgres timestamptz arrives with an offset; a naive value would otherwise
  // be read as local time and silently shift by the box's timezone.
  const withZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`
  const date = new Date(withZone)
  return Number.isNaN(date.getTime()) ? null : date
}

/** Absolute local time, e.g. "Jul 30 14:21". Year shown only when it differs. */
export function absoluteTime(value: string | null | undefined): string {
  const date = parse(value)
  if (!date) return '—'
  const sameYear = date.getFullYear() === new Date().getFullYear()
  return date.toLocaleString(undefined, {
    year: sameYear ? undefined : 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

/** "just now" / "12 min ago" / "3 h ago", falling back to a date past a day. */
export function relativeTime(value: string | null | undefined): string {
  const date = parse(value)
  if (!date) return '—'
  const delta = Date.now() - date.getTime()
  if (delta < 0) return absoluteTime(value) // scheduled in the future
  if (delta < MINUTE) return 'just now'
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)} min ago`
  if (delta < DAY) {
    const hours = Math.floor(delta / HOUR)
    return `${hours} h ago`
  }
  return absoluteTime(value)
}

/** Full timestamp for a tooltip — the precise value is still one hover away. */
export function exactTime(value: string | null | undefined): string {
  const date = parse(value)
  return date ? date.toString() : ''
}

/** "17m 23s" between two instants; the number people actually want from a run. */
export function duration(
  from: string | null | undefined,
  to: string | null | undefined,
): string {
  const start = parse(from)
  const end = parse(to)
  if (!start || !end) return '—'
  const seconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}
