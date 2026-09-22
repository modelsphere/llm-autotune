/** How a candidate compares to the production baseline it was measured against.
 *
 *  The API returns `vs_baseline` as a raw ratio — the candidate's score divided
 *  by the same-stage baseline's score. These helpers turn that ratio into the
 *  one thing an operator reads off a night: is this better or worse than what
 *  production runs today, and by how much. The sign convention is deliberate
 *  and does NOT depend on whether the objective is maximized or minimized —
 *  positive is always better than production, negative always worse — so a
 *  throughput win and a latency win both read "+x%". One source of truth for
 *  the table columns and the ScoreBars, which used to disagree (the bars
 *  measured distance from the best config, the table a ratio from the baseline).
 */

/** Did this candidate beat production? Direction-aware: on a maximize target a
 *  ratio ≥ 1 wins, on a minimize target ≤ 1 does. */
export function beatsBaselineRatio(
  ratio: number | null | undefined,
  direction?: string,
): boolean {
  if (ratio == null) return false
  return direction === 'minimize' ? ratio <= 1 : ratio >= 1
}

/** The signed percentage a ratio represents, positive = better than baseline.
 *  Null when there is no baseline ratio to render. */
export function baselineDeltaPct(
  ratio: number | null | undefined,
  direction?: string,
): number | null {
  if (ratio == null) return null
  const better = direction === 'minimize' ? 1 - ratio : ratio - 1
  return better * 100
}

/** That percentage as text: "+2.0%", "-6.2%", "≈0%" within noise, "—" when no
 *  baseline ran. The noise band avoids rendering a rounding artefact as
 *  "-0.0%", which would read as a regression that is not there. */
export function fmtBaselineDelta(
  ratio: number | null | undefined,
  direction?: string,
): string {
  const pct = baselineDeltaPct(ratio, direction)
  if (pct == null) return '—'
  if (Math.abs(pct) < 0.05) return '≈0%'
  return `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`
}
