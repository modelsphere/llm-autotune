import { ElMessage } from 'element-plus'

/** Copy text, with a fallback for the platform's normal deployment.
 *
 *  `navigator.clipboard` only exists in a secure context, and the platform is
 *  served over plain http on an intranet hostname — so on the machine people
 *  actually use it, the modern API is simply `undefined`. The old
 *  `execCommand('copy')` path still works there.
 */
export async function copyText(text: string, what = 'Copied'): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      ElMessage.success(what)
      return true
    }
  } catch {
    // fall through to the legacy path rather than giving up
  }
  try {
    const scratch = document.createElement('textarea')
    scratch.value = text
    // Off-screen but still focusable: a hidden element cannot be selected.
    scratch.style.position = 'fixed'
    scratch.style.opacity = '0'
    document.body.appendChild(scratch)
    scratch.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(scratch)
    if (ok) {
      ElMessage.success(what)
      return true
    }
  } catch {
    /* reported below */
  }
  ElMessage.error('Copy failed — select the text manually')
  return false
}
