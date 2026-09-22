/** What a status looks like — decided once.
 *
 *  Three pages each had their own copy of this mapping, and they had already
 *  drifted: a campaign's status was grey for both `draft` and `done`, so a
 *  campaign nobody had started looked exactly like one that had finished,
 *  and the detail page showed no colour at all. A status is a vocabulary, not
 *  a per-table styling decision.
 */

export type TagType = 'success' | 'warning' | 'danger' | 'info' | 'primary'

export interface StatusLook {
  type: TagType
  /** Extra class for the states Element Plus has no colour for. */
  cls: string
}

/** draft — written, never started. Distinct from `done` in both colour and
 *  meaning: one is work not begun, the other work completed. */
const CAMPAIGN: Record<string, StatusLook> = {
  draft: { type: 'info', cls: 'tag-draft' },
  active: { type: 'success', cls: '' },
  paused: { type: 'warning', cls: '' },
  done: { type: 'info', cls: '' },
}

export function campaignStatus(status: string): StatusLook {
  return CAMPAIGN[status] ?? { type: 'info', cls: '' }
}

/** Live run states, in the order a run passes through them. Also the list that
 *  decides whether a run can still be stopped. */
export const LIVE_RUN_STATES = [
  'pending',
  'launching',
  'waiting_ready',
  'health_check',
  'benching',
]

export function isLive(status: string): boolean {
  return LIVE_RUN_STATES.includes(status)
}

export function runStatus(status: string): StatusLook {
  if (status === 'succeeded') return { type: 'success', cls: '' }
  if (status === 'failed') return { type: 'danger', cls: '' }
  if (status === 'killed') return { type: 'warning', cls: '' }
  return { type: 'primary', cls: '' }
}

/** Run states read better as words than as identifiers. */
export function runLabel(status: string): string {
  return status.replace(/_/g, ' ')
}
