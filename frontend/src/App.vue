<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
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
/** The menu, with whatever installed plugins add, each group in `order`.
 *  The top row is where people work all day: campaigns, their runs, the
 *  production traffic they replay, the machines they run on. The Library holds what a campaign is built from,
 *  set up once and reused, each entry saying what it is. */
const NAV: NavItem[] = [
  { path: '/campaigns', label: 'nav.campaigns', group: 'top', order: 10 },
  { path: '/runs', label: 'nav.runs', group: 'top', order: 20 },
  { path: '/datasets', label: 'nav.datasets', group: 'top', order: 25 },
  { path: '/resources', label: 'nav.resources', group: 'top', order: 30 },
  ...(AGENT_REPORTS ? [{ path: '/reports', label: 'nav.reports', group: 'top' as const, order: 50 }] : []),
  { path: '/search-spaces', label: 'nav.searchSpaces', hint: 'nav.hint.searchSpaces', group: 'library', order: 10 },
  { path: '/objectives', label: 'nav.objectives', hint: 'nav.hint.objectives', group: 'library', order: 20 },
  { path: '/policies', label: 'nav.policies', hint: 'nav.hint.policies', group: 'library', order: 40 },
  { path: '/baselines', label: 'nav.baselines', hint: 'nav.hint.baselines', group: 'library', order: 50 },
]
function navOf(group: NavItem['group']): NavItem[] {
  return [...NAV.filter((n) => n.group === group), ...pluginNav(group)]
    .sort((a, b) => a.order - b.order)
}
const libraryNav = navOf('library')
const topNav = navOf('top')
/** A sub-menu does not light up from its child being active the way a
 *  top-level item does, so the Library is marked active by hand. */
const inLibrary = computed(() => libraryNav.some((n) => n.path === activeMenu.value))

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
  else if (command === 'settings') router.push('/settings')
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

/** The account menu's links navigate themselves. Element Plus's dropdown item
 *  ignores a click whose default was prevented (since 2.x), so leaving it to
 *  the menu's `command` made Account and API keys do nothing when clicked.
 *  Keyboard use still goes through `command`. */
const userMenu = ref<{ handleClose: () => void } | null>(null)
function onMenuLinkClick(event: MouseEvent, path: string) {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
    event.stopPropagation()
    return
  }
  event.preventDefault()
  userMenu.value?.handleClose()
  router.push(path)
}

function logout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <el-container>
    <el-header v-if="auth.isAuthenticated" class="topbar">
      <span class="brand">LLM AutoTune</span>
      <el-menu mode="horizontal" :default-active="activeMenu" :ellipsis="false" router
        class="nav-menu">
        <el-menu-item v-for="item in topNav" :key="item.path" :index="item.path">
          <a :href="href(item.path)" class="menu-link" @click="onNavLinkClick">{{
            t(item.label) }}</a>
        </el-menu-item>
        <el-sub-menu index="library" popper-class="library-menu" :class="{ 'is-active-group': inLibrary }">
          <template #title>{{ t('nav.library') }}</template>
          <el-menu-item v-for="item in libraryNav" :key="item.path" :index="item.path">
            <a :href="href(item.path)" class="menu-link" @click="onNavLinkClick">
              <span class="menu-label">{{ t(item.label) }}</span>
              <span v-if="item.hint" class="menu-hint">{{ t(item.hint) }}</span>
            </a>
          </el-menu-item>
        </el-sub-menu>
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
      <el-dropdown ref="userMenu" trigger="click" @command="onCommand">
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
                @click="onMenuLinkClick($event, '/account')">{{ t('nav.account') }}</a>
            </el-dropdown-item>
            <el-dropdown-item command="keys">
              <a :href="href('/api-keys')" class="dropdown-link"
                @click="onMenuLinkClick($event, '/api-keys')">{{ t('nav.apiKeys') }}</a>
            </el-dropdown-item>
            <el-dropdown-item v-if="auth.user?.role === 'admin'" command="settings">
              <a :href="href('/settings')" class="dropdown-link"
                @click="onMenuLinkClick($event, '/settings')">{{ t('nav.settings') }}</a>
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
  gap: 24px;
  height: 56px;
  background: var(--autotune-surface);
  border-bottom: 1px solid var(--autotune-border);
}
.brand {
  font-weight: 700;
  font-size: 16px;
  letter-spacing: -0.01em;
  white-space: nowrap;
}
.nav-menu {
  --el-menu-horizontal-height: 55px;
  --el-menu-item-font-size: 14px;
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
/* The Library's entries: a name and what it is, on two lines. */
.el-popper.library-menu .el-menu--popup .el-menu-item {
  height: auto;
  line-height: 1.35;
  padding-top: 8px;
  padding-bottom: 8px;
}
.library-menu a.menu-link {
  display: flex;
  flex-direction: column;
  min-width: 240px;
}
.library-menu .menu-label {
  font-size: 13.5px;
  font-weight: 500;
}
.library-menu .menu-hint {
  font-size: 12px;
  color: var(--autotune-muted);
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
