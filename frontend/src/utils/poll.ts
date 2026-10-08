/** A page's periodic refresh, tied to the page's life.
 *
 *  Every polling view used to do `timer = setInterval(load, 10_000)` at the end
 *  of an async onMounted and `clearInterval(timer)` in onUnmounted. On a slow
 *  network that leaked: leave the page while its first requests are still in
 *  flight and onUnmounted runs first (clearing nothing), then the awaits finish
 *  and start an interval nobody will ever stop. Each leaked poller kept
 *  requesting a page you had left, and enough of them fill the browser's few
 *  connections per host — so the NEXT page's requests and chunks queued
 *  behind them, and clicks on tabs seemed to do nothing.
 *
 *  This also skips a tick while the previous refresh is still running (a
 *  10-second poll whose refresh takes 12 seconds otherwise piles up), and
 *  pauses while the browser tab is hidden — refreshing at once when it is
 *  shown again — so background tabs do not compete with the one in use.
 */
import { onBeforeUnmount } from 'vue'

export function usePoll(refresh: () => unknown, intervalMs: number) {
  let timer = 0
  let alive = true
  let running = false

  async function tick() {
    if (!alive || running || document.hidden) return
    running = true
    try {
      await refresh()
    } catch {
      // A failed refresh is retried by the next tick; the page keeps what it has.
    } finally {
      running = false
    }
  }

  function onVisibility() {
    if (!document.hidden) void tick()
  }

  /** Start polling. A no-op once the page is gone, which is the point. */
  function start() {
    if (!alive || timer) return
    timer = window.setInterval(tick, intervalMs)
    document.addEventListener('visibilitychange', onVisibility)
  }

  function stop() {
    window.clearInterval(timer)
    timer = 0
    document.removeEventListener('visibilitychange', onVisibility)
  }

  onBeforeUnmount(() => {
    alive = false
    stop()
  })

  return { start, stop }
}
