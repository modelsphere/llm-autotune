import { ref } from 'vue'
import { createRouter, createWebHistory, type RouteLocationNormalized } from 'vue-router'
import { pluginRoutes } from '../plugins'
import { useAuthStore } from '../stores/auth'

export const AGENT_REPORTS = import.meta.env.VITE_AGENT_REPORTS === '1'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('../views/LoginView.vue') },
    { path: '/', redirect: '/campaigns' },
    { path: '/campaigns', component: () => import('../views/CampaignsView.vue') },
    { path: '/campaigns/new', component: () => import('../views/CampaignNewView.vue') },
    { path: '/campaigns/:id', component: () => import('../views/CampaignDetailView.vue') },
    { path: '/search-spaces', component: () => import('../views/SearchSpacesView.vue') },
    { path: '/objectives', component: () => import('../views/ObjectivesView.vue') },
    { path: '/policies', component: () => import('../views/PoliciesView.vue') },
    { path: '/baselines', component: () => import('../views/BaselinesView.vue') },
    { path: '/runs', component: () => import('../views/RunsView.vue') },
    { path: '/resources', component: () => import('../views/ResourcesView.vue') },
    { path: '/api-keys', component: () => import('../views/ApiKeysView.vue') },
    // Agent-written reports: off unless built with VITE_AGENT_REPORTS=1, as the
    // backend's agent API is off unless enabled.
    ...(AGENT_REPORTS
      ? [
          { path: '/reports', component: () => import('../views/ReportsView.vue') },
          { path: '/reports/:id', component: () => import('../views/ReportView.vue') },
        ]
      : []),
    // Reached from the user menu, not the main nav — somewhere you go once.
    { path: '/account', component: () => import('../views/AccountView.vue') },
    // Pages installed plugins add (src/plugins), in the order they list them.
    ...pluginRoutes(),
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  // Keep the page they were after: links into the app arrive from outside
  // (a benchmark submission pointing back at its run), and dropping the path
  // at the login door strands them on the campaigns list.
  if (to.path !== '/login' && !auth.isAuthenticated) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.path === '/login' && auth.isAuthenticated) return '/campaigns'
})

// -- feedback while a page is on its way -------------------------------------
// Every page is a lazily loaded chunk. On a slow network a click on the nav
// used to look like it did nothing until that chunk arrived; this flag drives
// the thin bar at the top of App.vue. It only shows after a short delay, so a
// navigation that is already instant does not flicker.
export const navigating = ref(false)
let navTimer = 0
router.beforeEach(() => {
  window.clearTimeout(navTimer)
  navTimer = window.setTimeout(() => (navigating.value = true), 120)
})
function navDone() {
  window.clearTimeout(navTimer)
  navigating.value = false
}
// afterEach also runs for aborted/cancelled navigations; a thrown error (a
// chunk that failed to load) skips it and lands in onError instead.
router.afterEach(navDone)

// -- a page chunk that will not load -----------------------------------------
// The platform is redeployed often, and a redeploy replaces every hashed chunk.
// A tab opened before it still holds the old index, so the next click asks for
// a chunk that no longer exists, the dynamic import rejects, and the
// navigation is silently dropped — "clicking does nothing". The cure is a
// fresh page load of where they were going, which fetches the new index. A
// guard in sessionStorage stops it looping when the chunk is really broken.
const CHUNK_ERROR = /dynamically imported module|Importing a module script failed|Unable to preload CSS|Loading (CSS )?chunk \d+ failed/i
const RELOAD_KEY = 'autotune_chunk_reload'

function isChunkError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')
  return CHUNK_ERROR.test(message)
}

router.onError((error: unknown, to: RouteLocationNormalized) => {
  navDone()
  if (!isChunkError(error)) return
  const target = router.resolve(to.fullPath).href
  let last: { href?: string; at?: number } = {}
  try {
    last = JSON.parse(sessionStorage.getItem(RELOAD_KEY) || '{}')
  } catch {
    last = {}
  }
  // Already reloaded for this very page a moment ago and it still fails:
  // a reload loop helps nobody; leave the error in the console.
  if (last.href === target && Date.now() - (last.at ?? 0) < 15_000) return
  try {
    sessionStorage.setItem(RELOAD_KEY, JSON.stringify({ href: target, at: Date.now() }))
  } catch {
    // Storage unavailable (private mode): reload anyway, once per click.
  }
  window.location.assign(target)
})
