<script setup lang="ts">
/** A baseline's deploy-repo binding: where its file lives, sync it, and
 *  decide — explicitly, once — what to do about every difference between the
 *  platform's copy and the file. The shortcuts ("ignore image", …) are the
 *  common decisions as one click.
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, ref } from 'vue'
import {
  api, type Baseline, type BaselineSync, type DeployFormatsCatalog, type Divergence,
} from '../api/client'
import { useI18n } from '../i18n'
import { exactTime, relativeTime } from '../utils/time'
import DeployBranchSelect from './DeployBranchSelect.vue'

const props = defineProps<{ baseline: Baseline; catalog: DeployFormatsCatalog | null }>()
const emit = defineEmits<{ (e: 'changed', baseline: Baseline): void }>()
const { t } = useI18n()

const editing = ref(false)
const busy = ref(false)
const form = ref({ project: '', branch: '', path: 'config/model.yaml', preset: '' })

const binding = computed(() => props.baseline.binding)
const unresolved = computed(() => binding.value?.divergences.filter((d) => d.status === 'unresolved') ?? [])
const decided = computed(() => binding.value?.divergences.filter((d) => d.status !== 'unresolved') ?? [])

function openEditor() {
  const b = binding.value
  form.value = {
    project: b?.project || props.catalog?.default_project || '',
    branch: b?.branch || '',
    path: b?.path || 'config/model.yaml',
    preset: b?.format?.preset || props.catalog?.default_preset || '',
  }
  editing.value = true
}

async function save() {
  if (!form.value.project.trim() || !form.value.branch.trim()) {
    ElMessage.error('Project and branch are required')
    return
  }
  busy.value = true
  try {
    const { data } = await api.put(`/baselines/${props.baseline.id}/binding`, {
      project: form.value.project.trim(), branch: form.value.branch.trim(),
      path: form.value.path.trim(), format: form.value.preset ? { preset: form.value.preset } : {},
    })
    emit('changed', data)
    editing.value = false
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail ?? 'Bind failed')
  } finally {
    busy.value = false
  }
}

async function unbind() {
  try {
    await ElMessageBox.confirm(t('binding.unbind') + '?', t('binding.title'), { type: 'warning' })
  } catch {
    return
  }
  const { data } = await api.delete(`/baselines/${props.baseline.id}/binding`)
  emit('changed', data)
}

async function sync() {
  busy.value = true
  try {
    const { data } = await api.post<BaselineSync>(`/baselines/${props.baseline.id}/binding/sync`)
    emit('changed', data.baseline)
    if (data.adopted.length) {
      ElMessage.info(t('binding.adopted', { keys: data.adopted.map((a) => a.key).join(', ') }))
    }
    ElMessage.success(data.unresolved ? t('binding.syncOk', { n: data.unresolved }) : t('binding.syncClean'))
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail ?? 'Sync failed')
  } finally {
    busy.value = false
  }
}

async function resolve(d: Divergence, action: string) {
  busy.value = true
  try {
    const { data } = await api.post(`/baselines/${props.baseline.id}/binding/resolve`, {
      kind: d.kind, key: d.key, action,
    })
    emit('changed', data)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail ?? 'Could not resolve')
  } finally {
    busy.value = false
  }
}

async function shortcut(name: string) {
  busy.value = true
  try {
    const { data } = await api.post(`/baselines/${props.baseline.id}/binding/policy`, { shortcut: name })
    emit('changed', data)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail ?? 'Could not apply')
  } finally {
    busy.value = false
  }
}

function fmt(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** Which shortcuts are already in force, so the buttons read as toggles. */
function active(name: string): boolean {
  const policy = binding.value?.policy ?? {}
  const fields = policy.fields ?? {}
  const knobs = policy.knobs ?? {}
  switch (name) {
    case 'ignore_image': return fields.image === 'ignore'
    case 'ignore_model_path': return fields.model_path === 'ignore'
    case 'ignore_env': return fields.extra_env === 'ignore'
    case 'ignore_volumes': return fields.extra_volumes === 'ignore'
    case 'ignore_gpus': return fields.gpus === 'ignore'
    case 'ignore_parsers': return knobs.reasoning_parser === 'ignore'
    case 'ignore_paths': return policy.path_knobs === 'ignore'
    case 'report_paths': return (policy.path_knobs ?? 'repo') === 'repo'
    case 'follow_image': return fields.image === 'platform'
    case 'follow_model_path': return fields.model_path === 'platform'
    default: return false
  }
}

const ownerRows = computed(() => {
  const policy = binding.value?.policy ?? {}
  const defaults = props.catalog?.default_policy ?? {}
  const rows = (props.catalog?.fields ?? []).map((f) => ({
    key: f, owner: policy.fields?.[f] ?? defaults[f] ?? 'repo', explicit: !!policy.fields?.[f],
  }))
  for (const [knob, owner] of Object.entries(policy.knobs ?? {})) rows.push({ key: `--${knob.replace(/_/g, '-')}`, owner, explicit: true })
  return rows
})

function ownerLabel(owner: string): string {
  return owner === 'platform' ? t('binding.ownerPlatform') : owner === 'ignore' ? t('binding.ownerIgnore') : t('binding.ownerRepo')
}
</script>

<template>
  <div class="panel">
    <div class="head">
      <b>{{ t('binding.title') }}</b>
      <template v-if="binding">
        <span class="mono tiny">{{ binding.project }} · {{ binding.branch }} · {{ binding.path }}</span>
        <span class="muted tiny" :title="binding.synced_at ? exactTime(binding.synced_at) : ''">
          <template v-if="binding.synced_at">
            {{ t('binding.synced', { when: relativeTime(binding.synced_at), commit: binding.commit.slice(0, 10) }) }}
          </template>
          <template v-else>{{ t('binding.neverSynced') }}</template>
        </span>
        <el-tag v-if="unresolved.length" size="small" type="warning">{{ t('binding.unresolved', { n: unresolved.length }) }}</el-tag>
        <el-tag v-else-if="binding.synced_at" size="small" type="success" effect="plain">{{ t('binding.allResolved') }}</el-tag>
        <span class="grow" />
        <el-button size="small" type="primary" plain :loading="busy" :disabled="catalog && !catalog.gitlab_configured" @click="sync">
          {{ t('binding.sync') }}</el-button>
        <el-button size="small" @click="openEditor">{{ t('binding.edit') }}</el-button>
        <el-button size="small" type="danger" plain @click="unbind">{{ t('binding.unbind') }}</el-button>
      </template>
      <template v-else>
        <span class="grow" />
        <el-button size="small" type="primary" plain @click="openEditor">{{ t('binding.bind') }}</el-button>
      </template>
    </div>
    <p v-if="catalog && !catalog.gitlab_configured" class="muted tiny">{{ t('binding.gitlabOff') }}</p>

    <template v-if="binding">
      <div v-if="unresolved.length" class="section">
        <div class="muted tiny">{{ t('binding.divergences') }}</div>
        <el-table :data="unresolved" size="small">
          <el-table-column label="" width="60">
            <template #default="{ row }"><span class="muted tiny">{{ row.kind }}</span></template>
          </el-table-column>
          <el-table-column label="" min-width="160">
            <template #default="{ row }"><span class="mono">{{ row.key }}</span></template>
          </el-table-column>
          <el-table-column :label="t('binding.ours')" min-width="160">
            <template #default="{ row }"><span class="mono tiny">{{ fmt(row.ours) }}</span></template>
          </el-table-column>
          <el-table-column :label="t('binding.theirs')" min-width="160">
            <template #default="{ row }"><span class="mono tiny">{{ fmt(row.theirs) }}</span></template>
          </el-table-column>
          <el-table-column label="" width="420">
            <template #default="{ row }">
              <el-button size="small" :disabled="busy" @click="resolve(row, 'adopt')">{{ t('binding.adopt') }}</el-button>
              <el-button size="small" :disabled="busy || row.ours == null || row.ours === ''" @click="resolve(row, 'equivalent')">{{ t('binding.equivalent') }}</el-button>
              <el-button size="small" :disabled="busy" @click="resolve(row, 'repo')">{{ t('binding.repoOwned') }}</el-button>
              <el-button size="small" :disabled="busy" @click="resolve(row, 'ignore')">{{ t('binding.ignore') }}</el-button>
              <el-button size="small" :disabled="busy" @click="resolve(row, 'platform')">{{ t('binding.platformOwned') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="section shortcuts">
        <span class="muted tiny">{{ t('binding.shortcuts') }}:</span>
        <el-button v-for="s in catalog?.shortcuts ?? []" :key="s.name" size="small" :title="s.hint"
          :type="active(s.name) ? 'primary' : ''" :plain="active(s.name)" :disabled="busy" @click="shortcut(s.name)">
          {{ s.label }}
        </el-button>
      </div>

      <details class="section">
        <summary class="muted tiny">{{ t('binding.policy') }} · {{ decided.length }} decided</summary>
        <div class="owners">
          <span v-for="row in ownerRows" :key="row.key" class="owner" :class="{ explicit: row.explicit }">
            <span class="mono">{{ row.key }}</span> → {{ ownerLabel(row.owner) }}
          </span>
        </div>
        <el-table v-if="decided.length" :data="decided" size="small">
          <el-table-column label="" min-width="160">
            <template #default="{ row }"><span class="mono">{{ row.key }}</span></template>
          </el-table-column>
          <el-table-column :label="t('binding.ours')" min-width="140">
            <template #default="{ row }"><span class="mono tiny">{{ fmt(row.ours) }}</span></template>
          </el-table-column>
          <el-table-column :label="t('binding.theirs')" min-width="140">
            <template #default="{ row }"><span class="mono tiny">{{ fmt(row.theirs) }}</span></template>
          </el-table-column>
          <el-table-column label="" min-width="160">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.status }}</el-tag>
              <span class="muted tiny"> {{ row.decided_by }}</span>
            </template>
          </el-table-column>
        </el-table>
      </details>
    </template>

    <el-dialog v-model="editing" :title="binding ? t('binding.edit') : t('binding.bind')" width="620px" append-to-body>
      <el-form label-position="top">
        <el-form-item :label="t('binding.project')">
          <el-input v-model="form.project" class="mono" placeholder="group/subgroup/project or numeric id" />
        </el-form-item>
        <el-form-item :label="t('binding.branch')">
          <DeployBranchSelect v-model="form.branch" :project="form.project" />
          <div class="muted tiny">{{ t('binding.branchHint') }}</div>
        </el-form-item>
        <div class="grid-2">
          <el-form-item :label="t('binding.path')">
            <el-input v-model="form.path" class="mono" />
          </el-form-item>
          <el-form-item :label="t('binding.preset')">
            <el-select v-model="form.preset" style="width: 100%">
              <el-option v-for="p in catalog?.presets ?? []" :key="p.name" :value="p.name" :label="p.label" />
            </el-select>
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="editing = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="busy" @click="save">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.panel { padding: 10px 12px; background: var(--el-fill-color-lighter); border-radius: 6px; }
.head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.grow { flex: 1; }
.section { margin-top: 10px; }
.shortcuts { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.owners { display: flex; flex-wrap: wrap; gap: 6px 14px; margin: 6px 0; font-size: 12px; }
.owner { color: var(--el-text-color-secondary); }
.owner.explicit { color: var(--el-text-color-primary); }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.tiny { font-size: 11.5px; }
</style>
