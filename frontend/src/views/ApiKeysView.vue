<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { api, type ApiKey, type ApiKeyCreated } from '../api/client'
import InfoHint from '../components/InfoHint.vue'
import { copyText } from '../utils/clipboard'
import { exactTime, relativeTime } from '../utils/time'

const openDocs = () => window.open('/api/docs', '_blank')

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

async function create() {
  if (!name.value.trim()) {
    ElMessage.error('Give the key a name — it is what appears in the audit trail')
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
    ElMessage.error(error.response?.data?.detail ?? 'Could not mint a key')
  } finally {
    busy.value = false
  }
}

async function revoke(key: ApiKey) {
  try {
    await ElMessageBox.confirm(
      `Revoke "${key.name}"? Anything still using it starts failing on its next ` +
        `request. This cannot be undone — mint a new key instead.`,
      'Revoke API key',
      { confirmButtonText: 'Revoke', cancelButtonText: 'Cancel', type: 'warning' },
    )
  } catch {
    return
  }
  await api.delete(`/api-keys/${key.id}`)
  await load()
}

const curlExample = (secret: string) =>
  `curl -X POST http://192.0.2.10:28100/api/machines/lease \\
  -H 'X-API-Key: ${secret}' \\
  -H 'Content-Type: application/json' \\
  -d '{"name": "node-24", "host": "198.51.100.24", "gpu_count": 8}'`

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1 class="page-title">API keys</h1>
        <span class="muted">
          Credentials for systems, not people — a fleet manager leasing machines to the
          platform.
          <InfoHint :width="380">
            A key acts with the authority of whoever minted it, and appears in the audit
            trail as <span class="mono">key:&lt;name&gt;</span> — never the key itself.
            Only the hash is stored, so a lost key is replaced, not recovered.
          </InfoHint>
        </span>
      </div>
      <div>
        <el-button @click="openDocs">API docs</el-button>
        <el-button type="primary" @click="showNew = true">Mint a key</el-button>
      </div>
    </div>

    <el-table :data="keys" empty-text="No keys yet">
      <el-table-column label="Name" min-width="180">
        <template #default="{ row }">
          <span :class="row.revoked_at ? 'dead' : ''">{{ row.name }}</span>
          <el-tag v-if="row.revoked_at" size="small" type="info" class="tag">revoked</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Key" min-width="160">
        <template #default="{ row }"><span class="mono muted">{{ row.masked }}</span></template>
      </el-table-column>
      <el-table-column label="Last used" min-width="140">
        <template #default="{ row }">
          <span v-if="row.last_used_at" :title="exactTime(row.last_used_at)">
            {{ relativeTime(row.last_used_at) }}
          </span>
          <span v-else class="muted">never</span>
        </template>
      </el-table-column>
      <el-table-column label="Created" min-width="130">
        <template #default="{ row }">
          <span :title="exactTime(row.created_at)">{{ relativeTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column width="110">
        <template #default="{ row }">
          <el-button v-if="!row.revoked_at" size="small" type="danger" plain
            @click="revoke(row)">
            Revoke
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="showNew" title="Mint an API key" width="460px">
      <el-form label-position="top">
        <el-form-item label="Name">
          <el-input v-model="name" placeholder="fleet-manager" @keyup.enter="create" />
          <div class="muted hint">
            Name it after the system that will hold it — that is how you will know what
            you are revoking later.
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showNew = false">Cancel</el-button>
        <el-button type="primary" :loading="busy" @click="create">Mint</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="!!minted" title="Copy this key now" width="640px"
      :close-on-click-modal="false" @update:model-value="minted = null">
      <el-alert type="warning" :closable="false" show-icon
        title="Shown once and never again"
        description="Only a hash is stored. If you lose this, revoke it and mint another." />
      <div class="secret mono">{{ minted?.secret }}</div>
      <el-button size="small" type="primary" plain
        @click="copyText(minted?.secret ?? '', 'Key copied')">Copy key</el-button>

      <p class="muted example-head">Leasing a machine with it:</p>
      <pre class="mono block">{{ curlExample(minted?.secret ?? '') }}</pre>

      <template #footer>
        <el-button type="primary" @click="minted = null">Done — I have saved it</el-button>
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
.hint {
  font-size: 12px;
  line-height: 1.5;
  margin-top: 4px;
}
.secret {
  background: #f1f5f9;
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
  background: #f1f5f9;
  padding: 12px;
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
</style>
