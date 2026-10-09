/** Navigation that behaves like a link.
 *
 *  A table row or a card cannot be an `<a>`, so clicking it used to be a bare
 *  `router.push` — and a middle-click or a Ctrl/Cmd+click on it did nothing,
 *  or worse, replaced the page you were on. These helpers give such elements
 *  the link contract: a plain click navigates in place, a middle-click or a
 *  Ctrl/Cmd+click opens the destination in a new tab.
 *
 *  Wherever the thing CAN be an anchor, use `<router-link>` instead (add the
 *  global `nav-link` class to keep the look of what it replaced): a real
 *  anchor also gets the browser's own "Open link in new tab" menu.
 */
import { useRouter, type RouteLocationRaw, type Router } from 'vue-router'

/** Did the click ask for a new tab rather than for this one? */
export function wantsNewTab(event?: MouseEvent | null): boolean {
  if (!event) return false
  return event.button === 1 || event.ctrlKey || event.metaKey
}

/** The click landed on a real link (a `<router-link>` in a name cell, an
 *  external `<a href>`): the browser and the link already handle it, and
 *  acting on it here as well would open the page twice. */
function onAnchor(event?: Event | null): boolean {
  const target = event?.target
  return target instanceof Element && !!target.closest('a[href]')
}

/** Middle-clicking a button inside a row means nothing; do not navigate. */
function onControl(event?: Event | null): boolean {
  const target = event?.target
  return target instanceof Element && !!target.closest('a[href], button, input, textarea, select')
}

/** Go to `to`: in a new tab when the click asked for one, else in place. */
export function navigate(router: Router, to: RouteLocationRaw, event?: MouseEvent | null): void {
  if (onAnchor(event)) return
  if (wantsNewTab(event)) window.open(router.resolve(to).href, '_blank', 'noopener')
  else void router.push(to)
}

/** Listeners that make any element (a card, a block) act as a link to `to`:
 *  `<el-card v-bind="linkTo(`/x/${id}`)">`. Middle-click fires `auxclick`,
 *  not `click`, so it needs its own listener; the middle-button mousedown is
 *  cancelled so Windows/Linux do not start auto-scroll instead. */
export function useLinkTo() {
  const router = useRouter()
  return (to: RouteLocationRaw) => ({
    onClick: (event: MouseEvent) => navigate(router, to, event),
    onAuxclick: (event: MouseEvent) => {
      if (event.button === 1 && !onControl(event)) navigate(router, to, event)
    },
    onMousedown: (event: MouseEvent) => {
      if (event.button === 1 && !onControl(event)) event.preventDefault()
    },
  })
}

/** Row navigation for an `el-table`: `<el-table v-bind="rows">` where
 *  `const rows = useRowLink((row: Campaign) => `/campaigns/${row.id}`)`.
 *
 *  el-table has no row event for the middle button, so the row under the
 *  pointer is tracked from `cell-mouse-enter` and a native `auxclick` on the
 *  table opens it. A row whose destination is `null` is not a link. */
export function useRowLink<T>(to: (row: T) => RouteLocationRaw | null | undefined) {
  const router = useRouter()
  let hovered: T | null = null
  function open(row: T | null, event: MouseEvent) {
    if (row == null) return
    const dest = to(row)
    if (dest != null) navigate(router, dest, event)
  }
  return {
    onRowClick: (row: T, _column: unknown, event: MouseEvent) => open(row, event),
    onCellMouseEnter: (row: T) => {
      hovered = row
    },
    onCellMouseLeave: () => {
      hovered = null
    },
    onAuxclick: (event: MouseEvent) => {
      if (event.button === 1 && !onControl(event)) open(hovered, event)
    },
    onMousedown: (event: MouseEvent) => {
      if (event.button === 1 && hovered != null && !onControl(event)) event.preventDefault()
    },
  }
}
