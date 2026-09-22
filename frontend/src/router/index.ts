import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

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
    { path: '/baselines', component: () => import('../views/BaselinesView.vue') },
    { path: '/runs', component: () => import('../views/RunsView.vue') },
    { path: '/resources', component: () => import('../views/ResourcesView.vue') },
    { path: '/api-keys', component: () => import('../views/ApiKeysView.vue') },
    { path: '/reports', component: () => import('../views/ReportsView.vue') },
    { path: '/reports/:id', component: () => import('../views/ReportView.vue') },
    // Reached from the user menu, not the main nav — somewhere you go once.
    { path: '/account', component: () => import('../views/AccountView.vue') },
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
