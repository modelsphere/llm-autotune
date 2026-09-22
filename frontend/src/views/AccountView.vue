<script setup lang="ts">
/** Your account: who you are, your password, and — for an admin — who else
 *  there is.
 *
 *  Reached from the user menu rather than the main nav: it is somewhere you go
 *  once, not somewhere you work. API keys stay a page of their own because they
 *  are a working surface, and it is linked from here for the same reason it is
 *  linked from the menu.
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/auth'
import { exactTime, relativeTime } from '../utils/time'

interface AccountUser {
  id: number
  username: string
  role: string
  created_at: string | null
  /** The name this user's submissions are published under on the benchmark
   *  platform. A display name, not a credential. */
  contributor?: string
}

const auth = useAuthStore()
const router = useRouter()
const { t } = useI18n()

const me = ref<AccountUser | null>(null)
const users = ref<AccountUser[]>([])
const busy = ref(false)

const isAdmin = computed(() => me.value?.role === 'admin')
const adminCount = computed(() => users.value.filter((u) => u.role === 'admin').length)

const form = ref({ current: '', next: '', confirm: '' })

const publishBusy = ref(false)
const contributor = ref('')

async function saveContributor() {
  const name = contributor.value.trim()
  if (!name) return
  publishBusy.value = true
  try {
    me.value = (await api.put('/auth/me/contributor', { contributor: name })).data
    ElMessage.success(t('account.publish.nameSaved'))
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? t('account.publish.saveFailed'))
  } finally {
    publishBusy.value = false
  }
}

async function clearContributor() {
  publishBusy.value = true
  try {
    me.value = (await api.delete('/auth/me/contributor')).data
    contributor.value = ''
    ElMessage.success(t('account.publish.nameCleared'))
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? t('account.publish.saveFailed'))
  } finally {
    publishBusy.value = false
  }
}

/** What is stopping the change, in the words of the thing that is wrong. Shown
 *  before the request rather than after it, since every one of these is
 *  knowable here. */
const passwordProblem = computed(() => {
  if (!form.value.current) return t('account.needCurrent')
  if (form.value.next.length < 6) return t('account.tooShort')
  if (form.value.next !== form.value.confirm) return t('account.mismatch')
  if (form.value.next === form.value.current) return t('account.sameAsOld')
  return ''
})

async function load() {
  me.value = (await api.get('/auth/me')).data
  // Unlike the token, this one round-trips — show what entries actually say.
  contributor.value = me.value?.contributor ?? ''
  if (isAdmin.value) users.value = (await api.get('/auth/users')).data
}

async function changePassword() {
  if (passwordProblem.value) return
  busy.value = true
  try {
    const { data } = await api.post('/auth/password', {
      current_password: form.value.current,
      new_password: form.value.next,
    })
    // The server hands back a fresh token; keep using it so this tab does not
    // quietly hold a credential minted for the old password.
    auth.setToken(data.access_token)
    form.value = { current: '', next: '', confirm: '' }
    ElMessage.success(t('account.changed'))
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? t('account.changeFailed'))
  } finally {
    busy.value = false
  }
}

async function setRole(user: AccountUser, role: string) {
  try {
    await api.put(`/auth/users/${user.id}/role`, { role })
    await load()
    ElMessage.success(t('account.roleChanged', { user: user.username, role }))
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail ?? t('account.roleFailed'))
  }
}

/** The last admin has no one to hand over to, so the control is disabled with
 *  a reason rather than failing on submit. */
function lockedReason(user: AccountUser): string {
  if (user.role === 'admin' && adminCount.value <= 1) return t('account.lastAdmin')
  return ''
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h1 class="page-title">{{ t('account.title') }}</h1>

    <div class="cards">
      <el-card class="who">
        <h3>{{ t('account.profile') }}</h3>
        <dl>
          <dt>{{ t('login.username') }}</dt>
          <dd>{{ me?.username ?? '—' }}</dd>
          <dt>{{ t('account.role') }}</dt>
          <dd>
            <el-tag size="small" :type="isAdmin ? 'success' : 'info'" effect="plain">
              {{ me?.role ?? '—' }}
            </el-tag>
          </dd>
          <dt>{{ t('common.created') }}</dt>
          <dd :title="exactTime(me?.created_at ?? null)">
            {{ me?.created_at ? relativeTime(me.created_at) : '—' }}
          </dd>
        </dl>
        <el-button text type="primary" @click="router.push('/api-keys')">
          {{ t('account.manageKeys') }}
        </el-button>
      </el-card>

      <el-card class="password">
        <h3>{{ t('account.changePassword') }}</h3>
        <p class="muted tiny">{{ t('account.tokenNote') }}</p>
        <el-form label-position="top" @submit.prevent="changePassword">
          <el-form-item :label="t('account.currentPassword')">
            <el-input v-model="form.current" type="password" autocomplete="current-password" />
          </el-form-item>
          <el-form-item :label="t('account.newPassword')">
            <el-input v-model="form.next" type="password" autocomplete="new-password" />
          </el-form-item>
          <el-form-item :label="t('account.confirmPassword')">
            <el-input v-model="form.confirm" type="password" autocomplete="new-password"
              @keyup.enter="changePassword" />
          </el-form-item>
          <div class="row">
            <el-button type="primary" :loading="busy" :disabled="!!passwordProblem"
              @click="changePassword">
              {{ t('account.changePassword') }}
            </el-button>
            <span v-if="passwordProblem" class="muted tiny">{{ passwordProblem }}</span>
          </div>
        </el-form>
      </el-card>

      <el-card class="publish">
        <h3>{{ t('account.publish.title') }}</h3>
        <p class="muted tiny">{{ t('account.publish.note') }}</p>
        <el-form label-position="top" @submit.prevent="saveContributor">
          <el-form-item :label="t('account.publish.nameField')">
            <el-input v-model="contributor" :placeholder="me?.username ?? ''"
              maxlength="255" @keyup.enter="saveContributor" />
            <div class="muted tiny">{{ t('account.publish.nameNote') }}</div>
          </el-form-item>
          <div class="row">
            <el-button :loading="publishBusy"
              :disabled="!contributor.trim() || contributor.trim() === me?.contributor"
              @click="saveContributor">
              {{ t('account.publish.nameSave') }}
            </el-button>
            <el-button v-if="me?.contributor" text :loading="publishBusy"
              @click="clearContributor">
              {{ t('account.publish.nameClear') }}
            </el-button>
          </div>
        </el-form>
      </el-card>
    </div>

    <template v-if="isAdmin">
      <h3 class="users-head">
        {{ t('account.users') }}
        <span class="muted tiny">{{ t('account.usersNote') }}</span>
      </h3>
      <el-table :data="users" size="small">
        <el-table-column prop="id" :label="t('common.id')" width="70" />
        <el-table-column prop="username" :label="t('login.username')" min-width="180" />
        <el-table-column :label="t('account.role')" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.role === 'admin' ? 'success' : 'info'"
              effect="plain">
              {{ row.role }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('common.created')" min-width="140">
          <template #default="{ row }">
            <span :title="exactTime(row.created_at)">{{ relativeTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column width="180" align="right">
          <template #default="{ row }">
            <el-tooltip :disabled="!lockedReason(row)" :content="lockedReason(row)"
              placement="top">
              <span>
                <el-button size="small" plain :disabled="!!lockedReason(row)"
                  @click="setRole(row, row.role === 'admin' ? 'user' : 'admin')">
                  {{ row.role === 'admin' ? t('account.demote') : t('account.promote') }}
                </el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.saved {
  color: var(--el-color-success);
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
  align-items: start;
}
h3 {
  margin: 0 0 10px;
  font-size: 14px;
}
dl {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 6px 12px;
  margin: 0 0 12px;
  font-size: 13px;
}
dt {
  color: var(--autotune-muted);
}
dd {
  margin: 0;
}
.row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.users-head {
  margin-top: 26px;
  display: flex;
  align-items: baseline;
  gap: 10px;
}
</style>
