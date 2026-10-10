<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n, type Locale } from './i18n'
import { pluginNav, type NavItem } from './plugins'
import { AGENT_REPORTS, navigating } from './router'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const { t, locale, setLocale } = useI18n()

const activeMenu = computed(() => '/' + route.path.split('/')[1])
/** The pages that live under the tuning menu. A sub-menu does not light up
 *  from its child being active the way a top-level item does, and a nav that
 *  forgets where you are is worse than one with more entries — so the group
 *  is marked active by hand. */
/** The menu, with whatever installed plugins add, each group in `order`. */
const NAV: NavItem[] = [
  { path: '/campaigns', label: 'nav.campaigns', group: 'tuning', order: 10 },
  { path: '/search-spaces', label: 'nav.searchSpaces', group: 'tuning', order: 30 },
  { path: '/objectives', label: 'nav.objectives', group: 'tuning', order: 40 },
  { path: '/policies', label: 'nav.policies', group: 'tuning', order: 45 },
  { path: '/baselines', label: 'nav.baselines', group: 'tuning', order: 50 },
  { path: '/runs', label: 'nav.runs', group: 'top', order: 20 },
  { path: '/resources', label: 'nav.resources', group: 'top', order: 30 },
  ...(AGENT_REPORTS ? [{ path: '/reports', label: 'nav.reports', group: 'top' as const, order: 50 }] : []),
]
function navOf(group: NavItem['group']): NavItem[] {
  return [...NAV.filter((n) => n.group === group), ...pluginNav(group)]
    .sort((a, b) => a.order - b.order)
}
const tuningNav = navOf('tuning')
const topNav = navOf('top')
const inTuning = computed(() => tuningNav.some((n) => n.path === activeMenu.value))

/** The store only learned who you are at login, so a reload left `user` null
 *  and the menu had no name to show. Fetch it whenever there is a token and no
 *  user — on start, and again after logging back in. A token the server no
 *  longer accepts is a session that has ended, so say so rather than sitting
 *  there with a broken menu. */
async function ensureUser() {
  if (!auth.isAuthenticated || auth.user) return
  try {
    await auth.fetchMe()
  } catch {
    auth.logout()
    router.push('/login')
  }
}
onMounted(ensureUser)
watch(() => auth.token, ensureUser)

const initial = computed(() => (auth.user?.username ?? '?').charAt(0).toUpperCase())

function onCommand(command: string) {
  if (command === 'account') router.push('/account')
  else if (command === 'keys') router.push('/api-keys')
  else if (command === 'logout') logout()
}

/** The nav entries (and the account menu's pages) are <a href>s so a
 *  middle-click, a Ctrl/Cmd+click or the browser's "Open link in new tab"
 *  work on them. A plain click is left to the menu (router mode / command,
 *  which also covers keyboard use), so the anchor cancels its own default; a
 *  modified click is the browser's, and must not reach the menu, or it would
 *  mark that entry active in THIS tab. */
function href(path: string): string {
  return router.resolve(path).href
}
function onNavLinkClick(event: MouseEvent) {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) event.stopPropagation()
  else event.preventDefault()
}

function logout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <el-container>
    <el-header v-if="auth.isAuthenticated" class="topbar">
      <span class="brand">LLM Autotune</span>
      <!-- Four entries, not seven. The tuning stack is one activity — a
           campaign searches a space against an objective, from a baseline —
           so it folds into one menu, and what is left on the top row are the
           things that are their own thing: the runs, the machines, and the
           when enabled, the reports written from them). -->
      <el-menu mode="horizontal" :default-active="activeMenu" :ellipsis="false" router
        class="nav-menu">
        <el-sub-menu index="tuning" :class="{ 'is-active-group': inTuning }">
          <template #title>{{ t('nav.tuning') }}</template>
          <el-menu-item v-for="item in tuningNav" :key="item.path" :index="item.path">
            <a :href="href(item.path)" class="menu-link" @click="onNavLinkClick">{{
              t(item.label) }}</a>
          </el-menu-item>
        </el-sub-menu>
        <el-menu-item v-for="item in topNav" :key="item.path" :index="item.path">
          <a :href="href(item.path)" class="menu-link" @click="onNavLinkClick">{{
            t(item.label) }}</a>
        </el-menu-item>
      </el-menu>
      <span class="spacer" />
      <!-- Two languages, so a segmented control rather than a dropdown: the
           choice is visible and one click away, not hidden behind a menu. -->
      <el-radio-group :model-value="locale" size="small" class="lang"
        :aria-label="t('nav.language')"
        @update:model-value="(v: string | number | boolean) => setLocale(v as Locale)">
        <el-radio-button value="en">EN</el-radio-button>
        <el-radio-button value="zh">中文</el-radio-button>
      </el-radio-group>
      <!-- API keys moved in here from the main nav: it is an account-level
           credential, not one of the working pages, and the top row is for the
           things people move between all day. -->
      <el-dropdown trigger="click" @command="onCommand">
        <span class="user-trigger">
          <span class="avatar">{{ initial }}</span>
          <span class="who">{{ auth.user?.username ?? '' }}</span>
          <span class="caret">▾</span>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <!-- One span, not three siblings: el-dropdown-item lays its content
                 out as a flex row, so the whitespace between a text node and a
                 <b> is dropped and it read "Signed in asalice". Inside a
                 single element it is ordinary inline text again. Styles are
                 inline because the menu is teleported out of this component,
                 where scoped selectors no longer reach it. -->
            <el-dropdown-item disabled>
              <span style="font-size: 12px">
                {{ t('nav.signedInAs') }} <b>{{ auth.user?.username ?? '' }}</b>
                <el-tag v-if="auth.user?.role" size="small" effect="plain"
                  style="margin-left: 6px">{{ auth.user.role }}</el-tag>
              </span>
            </el-dropdown-item>
            <el-dropdown-item divided command="account">
              <a :href="href('/account')" class="dropdown-link"
                @click="onNavLinkClick">{{ t('nav.account') }}</a>
            </el-dropdown-item>
            <el-dropdown-item command="keys">
              <a :href="href('/api-keys')" class="dropdown-link"
                @click="onNavLinkClick">{{ t('nav.apiKeys') }}</a>
            </el-dropdown-item>
            <el-dropdown-item divided command="logout">
              {{ t('nav.logOut') }}
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </el-header>
    <!-- A page chunk on its way: without this a click on a slow network looks
         like it did nothing. -->
    <div v-show="navigating" class="nav-progress" />
    <el-main>
      <!-- Keyed by path, so /campaigns/1 → /campaigns/2 (or back/forward
           between two runs) mounts the page afresh. Views load
           in onMounted and read their id once; a reused instance kept showing
           — and polling — the page you had left. Query changes (?tab=) do not
           remount. -->
      <router-view :key="route.path" />
    </el-main>
  </el-container>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  background: #fff;
  border-bottom: 1px solid var(--autotune-border);
}
.brand {
  font-weight: 700;
  font-size: 16px;
  white-space: nowrap;
}
.nav-menu {
  border-bottom: none;
}
/* Element's sub-menu title only colours itself when the menu is OPEN, so a
   group whose child page you are on looks unvisited. Same treatment a top-level
   active item gets: the colour and the underline. */
.nav-menu :deep(.is-active-group > .el-sub-menu__title) {
  color: var(--el-menu-active-color);
  border-bottom: 2px solid var(--el-menu-active-color);
}
.spacer {
  flex: 1;
}
.nav-progress {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  z-index: 3000;
  overflow: hidden;
  pointer-events: none;
}
.nav-progress::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  width: 40%;
  background: var(--el-color-primary);
  animation: nav-progress 1.1s ease-in-out infinite;
}
@keyframes nav-progress {
  from { left: -40%; }
  to { left: 100%; }
}
.lang {
  margin-right: 4px;
}
.user-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
  outline: none;
  transition: background 0.15s;
}
.user-trigger:hover {
  background: var(--autotune-bg, #f5f7fa);
}
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
}
.who {
  font-size: 13px;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.caret {
  color: var(--autotune-muted);
  font-size: 10px;
}

</style>

<!-- Not scoped: the tuning sub-menu and the account menu are teleported to
     <body>, and these must style the links inside them too. -->
<style>
/* The anchor inside each nav entry: looks like the entry's text, and its
   ::after covers the whole entry (the <li> is position: relative), so a
   middle-click anywhere on the entry lands on the link. */
a.menu-link {
  color: inherit;
  text-decoration: none;
}
a.menu-link::after {
  content: '';
  position: absolute;
  inset: 0;
}
/* Same for the account menu's links: the item's padding is part of the link. */
a.dropdown-link {
  color: inherit;
  text-decoration: none;
  margin: -5px -16px;
  padding: 5px 16px;
  flex: 1;
}
</style>
