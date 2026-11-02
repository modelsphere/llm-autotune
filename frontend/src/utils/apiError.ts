/** A readable line from a FastAPI error response.
 *
 *  `detail` is a string for HTTPException — but for schema validation it is an
 *  ARRAY of {loc, msg} objects, and feeding that to ElMessage renders an empty
 *  red toast: the error existed, the user saw nothing. This flattens both
 *  shapes; the fallback covers network failures with no response at all.
 */
export function apiErrorText(error: unknown, fallback: string): string {
  const detail = (error as any)?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  // A structured refusal carries its sentence as `message`, next to the data
  // a dialog shows (the runs in the way, a probe's findings).
  if (typeof detail?.message === 'string' && detail.message) return detail.message
  if (Array.isArray(detail)) {
    const lines = detail
      .map((d: any) => {
        const loc = Array.isArray(d?.loc)
          ? d.loc.filter((part: unknown) => part !== 'body').join('.')
          : ''
        // A model-level check arrives as "Value error, <what we said>".
        const msg = String(d?.msg ?? '').replace(/^Value error, /, '')
        return loc ? `${loc}: ${msg}` : msg
      })
      .filter(Boolean)
    if (lines.length) return lines.join('; ')
  }
  return fallback
}
