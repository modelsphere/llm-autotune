<script setup lang="ts">
/** API keys: mint, list and revoke the keys that act as you, and check that a
 *  key works. The check is one call that only asks who the key belongs to
 *  (GET /api/auth/me), shown as a command to copy and as a button that makes
 *  the same call from this page. */
import { useAuthStore } from '../stores/auth'
import { apiErrorText } from '../utils/apiError'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import axios from 'axios'
import { api, type ApiKey, type ApiKeyCreated } from '../api/client'
import CopyButton from '../components/CopyButton.vue'
import InfoHint from '../components/InfoHint.vue'
import { useI18n } from '../i18n'
import { copyText } from '../utils/clipboard'
import { exactTime, relativeTime } from '../utils/time'
import { useFirstLoad } from '../utils/load'

const { t } = useI18n()
const openDocs = () => window.open('/api/docs', '_blank')

const auth = useAuthStore()
const keys = ref<ApiKey[]>([])
const showNew = ref(false)
const busy = ref(false)
const name = ref('')

/** Held only until the dialog closes. There is no second chance to read it,
 *  so the dialog says so and refuses to be dismissed by a stray backdrop click. */
const minted = ref<ApiKeyCreated | null>(null)

async function load() {
  keys.value = (await api.get('/api-keys')).data
}
const first = useFirstLoad(load)

async function create() {
  if (!name.value.trim()) {
    ElMessage.error(t('apiKeys.needName'))
    return
  }
  busy.value = true
  try {
    const { data } = await api.post('/api-keys', { name: name.value.trim() })
    minted.value = data
    showNew.value = false
    name.value = ''
    await load()
  } catch (error: any) {
    ElMessage.error(apiErrorText(error, t('apiKeys.mintFailed')))
  } finally {
    busy.value = false
  }
}

async function revoke(key: ApiKey) {
  try {
    await ElMessageBox.confirm(
      t('apiKeys.revokeConfirm', { name: key.name }),
      t('apiKeys.revokeTitle'),
      { confirmButtonText: t('apiKeys.revoke'), cancelButtonText: t('common.cancel'), type: 'warning' },
    )
  } catch {
    return
  }
  await api.delete(`/api-keys/${key.id}`)
  await load()
}

// This platform's own address, as the browser reaches it: a fixed example
// address had to be noticed and replaced before the call worked.
const whoAmI = (secret: string) =>
  `curl ${window.location.origin}/api/auth/me \\
  -H 'X-API-Key: ${secret}'`
const leaseExample = (secret: string) =>
  `curl -X POST ${window.location.origin}/api/machines/lease \\
  -H 'X-API-Key: ${secret}' \\
  -H 'Content-Type: application/json' \\
  -d '{"name": "node-24", "host": "198.51.100.24", "gpu_count": 8}'`

// Try a key from this page: the same call, sent without the browser's own
// sign-in, so the answer is the key's and nobody else's.
const tryKey = ref('')
const tryResult = ref<{ ok: boolean; text: string } | null>(null)
const trying = ref(false)
const tryCommand = computed(() => whoAmI(tryKey.value.trim() || '<your key>'))
async function runTry() {
  trying.value = true
  tryResult.value = null
  try {
    const { data } = await axios.get('/api/auth/me', { headers: { 'X-API-Key': tryKey.value.trim() } })
    tryResult.value = { ok: true, text: t('apiKeys.tryOk', { user: data.username, role: data.role }) }
  } catch (error: any) {
    tryResult.value = { ok: false, text: t('apiKeys.tryBad', { why: apiErrorText(error, String(error)) }) }
  } finally {
    trying.value = false
  }
}

onMounted(first.run)
</script>

<template>
  <div class="page">
    <div class="header-row">
      <h1 class="page-title">{{ t('apiKeys.title') }}
        <InfoHint :width="380">{{ t('apiKeys.about') }}</InfoHint>
      </h1>
      <div>
        <el-button @click="openDocs">{{ t('apiKeys.docs') }}</el-button>
        <el-button type="primary" @click="showNew = true">{{ t('apiKeys.mint') }}</el-button>
      </div>
    </div>

    <el-table :data="keys" v-loading="first.loading" :empty-text="first.emptyText(t('apiKeys.empty'))">
      <el-table-column :label="t('apiKeys.name')" min-width="180">
        <template #default="{ row }">
          <span :class="row.revoked_at ? 'dead' : ''">{{ row.name }}</span>
          <el-tag v-if="row.revoked_at" size="small" type="info" class="tag">{{ t('apiKeys.revoked') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="keys.some((k) => k.owner && k.owner !== auth.user?.username)"
        :label="t('apiKeys.owner')" min-width="110">
        <template #default="{ row }">{{ row.owner }}</template>
      </el-table-column>
      <el-table-column :label="t('apiKeys.key')" min-width="160">
        <template #default="{ row }"><span class="mono muted">{{ row.masked }}</span></template>
      </el-table-column>
      <el-table-column :label="t('apiKeys.lastUsed')" min-width="140">
        <template #default="{ row }">
          <span v-if="row.last_used_at" :title="exactTime(row.last_used_at)">
            {{ relativeTime(row.last_used_at) }}
          </span>
          <span v-else class="muted">{{ t('apiKeys.never') }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.created')" min-width="130">
        <template #default="{ row }">
          <span :title="exactTime(row.created_at)">{{ relativeTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column width="110" align="right">
        <template #default="{ row }">
          <el-button v-if="!row.revoked_at" size="small" type="danger" plain @click="revoke(row)">
            {{ t('apiKeys.revoke') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-card shadow="never" class="try">
      <h3>{{ t('apiKeys.tryTitle') }}</h3>
      <p class="muted small">{{ t('apiKeys.tryAbout') }}</p>
      <div class="cmd">
        <pre class="mono block">{{ tryCommand }}</pre>
        <CopyButton :value="tryCommand" :label="t('apiKeys.copied')" />
      </div>
      <div class="try-row">
        <el-input v-model="tryKey" class="mono" :placeholder="t('apiKeys.tryPlaceholder')" clearable
          @keyup.enter="tryKey.trim() && runTry()" />
        <el-button :disabled="!tryKey.trim()" :loading="trying" @click="runTry">
          {{ t('apiKeys.tryButton') }}
        </el-button>
      </div>
      <p v-if="tryResult" :class="tryResult.ok ? 'ok' : 'bad'">{{ tryResult.text }}</p>
    </el-card>

    <el-dialog v-model="showNew" :title="t('apiKeys.mintTitle')" width="460px">
      <el-form label-position="top" @submit.prevent="create">
        <el-form-item :label="t('apiKeys.name')">
          <el-input v-model="name" placeholder="fleet-manager" @keyup.enter="create" />
          <div class="muted small">{{ t('apiKeys.nameHint') }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showNew = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="busy" @click="create">{{ t('apiKeys.mintButton') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="!!minted" :title="t('apiKeys.copyNow')" width="640px"
      :close-on-click-modal="false" @update:model-value="minted = null">
      <el-alert type="warning" :closable="false" show-icon :title="t('apiKeys.shownOnce')" />
      <div class="secret mono">{{ minted?.secret }}</div>
      <el-button size="small" type="primary" plain
        @click="copyText(minted?.secret ?? '', t('apiKeys.copied'))">{{ t('apiKeys.copyKey') }}</el-button>

      <p class="muted example-head">{{ t('apiKeys.tryAbout') }}</p>
      <pre class="mono block">{{ whoAmI(minted?.secret ?? '') }}</pre>
      <p class="muted example-head">{{ t('apiKeys.leaseExample') }}</p>
      <pre class="mono block">{{ leaseExample(minted?.secret ?? '') }}</pre>

      <template #footer>
        <el-button type="primary" @click="minted = null">{{ t('apiKeys.saved') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 24px;
  margin-bottom: 12px;
}
.tag {
  margin-left: 8px;
}
.dead {
  text-decoration: line-through;
  color: var(--autotune-muted);
}
.small {
  font-size: 12px;
  line-height: 1.5;
}
.try {
  margin-top: 20px;
}
.try h3 {
  margin: 0 0 6px;
  font-size: 14px;
}
.cmd {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin: 8px 0 12px;
}
.cmd pre {
  flex: 1;
}
.try-row {
  display: flex;
  gap: 8px;
  max-width: 640px;
}
.ok {
  color: var(--el-color-success);
  margin: 10px 0 0;
}
.bad {
  color: var(--el-color-danger);
  margin: 10px 0 0;
}
.secret {
  background: var(--el-fill-color-light);
  border: 1px solid var(--autotune-border);
  border-radius: 6px;
  padding: 12px;
  margin: 14px 0 8px;
  word-break: break-all;
  font-size: 13px;
}
.example-head {
  margin: 18px 0 6px;
}
.block {
  background: var(--el-fill-color-light);
  padding: 12px;
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
</style>
