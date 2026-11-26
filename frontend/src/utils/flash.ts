import { ref } from 'vue'

/** A short highlight on a field the page just changed for the user, so a
 *  change made somewhere else on the page is seen where it landed. Firing
 *  again restarts it. */
export function useFlash(ms = 1600) {
  const on = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  function fire() {
    on.value = false
    clearTimeout(timer)
    requestAnimationFrame(() => {
      on.value = true
      timer = setTimeout(() => (on.value = false), ms)
    })
  }
  return { on, fire }
}
