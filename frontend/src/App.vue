<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n, type Locale } from './i18n'
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
const TUNING = ['/campaigns', '/search-spaces', '/objectives', '/baselines']
const inTuning = computed(() => TUNING.includes(activeMenu.value))

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
           reports written from them. -->
      <el-menu mode="horizontal" :default-active="activeMenu" :ellipsis="false" router
        class="nav-menu">
        <el-sub-menu index="tuning" :class="{ 'is-active-group': inTuning }">
          <template #title>{{ t('nav.tuning') }}</template>
          <el-menu-item index="/campaigns">{{ t('nav.campaigns') }}</el-menu-item>
          <el-menu-item index="/search-spaces">{{ t('nav.searchSpaces') }}</el-menu-item>
          <el-menu-item index="/objectives">{{ t('nav.objectives') }}</el-menu-item>
          <el-menu-item index="/baselines">{{ t('nav.baselines') }}</el-menu-item>
        </el-sub-menu>
        <el-menu-item index="/runs">{{ t('nav.runs') }}</el-menu-item>
        <el-menu-item index="/resources">{{ t('nav.resources') }}</el-menu-item>
        <el-menu-item index="/reports">{{ t('nav.reports') }}</el-menu-item>
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
                 <b> is dropped and it read "Signed in assunjichen". Inside a
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
            <el-dropdown-item divided command="account">{{ t('nav.account') }}</el-dropdown-item>
            <el-dropdown-item command="keys">{{ t('nav.apiKeys') }}</el-dropdown-item>
            <el-dropdown-item divided command="logout">
              {{ t('nav.logOut') }}
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </el-header>
    <el-main>
      <router-view />
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
