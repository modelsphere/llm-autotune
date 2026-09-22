<script setup lang="ts">
/** A winner's configuration as a change request against the deploy file —
 *  previewed before anything is written.
 *
 *  The parent gives the preview and promote URLs and the base body; the dialog
 *  shows the knob-level change table, what the platform deliberately will not
 *  touch, the diff, and opens (or, in dry-run, records) the request on confirm.
 */
import { ElMessage } from 'element-plus'
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api, type MergeRequestPreview, type Promotion } from '../api/client'
import { useI18n } from '../i18n'
import { copyText } from '../utils/clipboard'
import DeployBranchSelect from './DeployBranchSelect.vue'

const props = defineProps<{
  modelValue: boolean
  previewUrl: string
  promoteUrl: string
  body?: Record<string, unknown>
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', open: boolean): void
  (e: 'promoted', promotion: Promotion): void
}>()

const { t } = useI18n()
const router = useRouter()

const loading = ref(false)
const busy = ref(false)
const preview = ref<MergeRequestPreview | null>(null)
const error = ref('')
const result = ref<Promotion | null>(null)
const applyRemovals = ref(false)
const promoteImage = ref(false)
const promoteModelPath = ref(false)
const promoteGpus = ref(false)
const showDescription = ref(false)
/** The release branch this merge request goes onto. The repo keeps one per
 *  model × card × engine, so the campaign (or the bound baseline) resolves a
 *  default — held in `resolved` — and the picker may override it for this one
 *  merge request. Only a real override is sent, so that promoting never
 *  silently pins a campaign to a branch nobody chose. */
const branch = ref('')
const resolved = ref('')
let adopting = false

const open = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

function requestBody(): Record<string, unknown> {
  const promote_fields: string[] = []
  if (promoteImage.value) promote_fields.push('image')
  if (promoteModelPath.value) promote_fields.push('model_path')
  if (promoteGpus.value) promote_fields.push('gpus')
  const override = branch.value && branch.value !== resolved.value ? { branch: branch.value } : {}
  return {
    ...(props.body ?? {}),
    ...override,
    apply_removals: applyRemovals.value,
    promote_fields,
  }
}

async function load() {
  loading.value = true
  error.value = ''
  result.value = null
  try {
    const { data } = await api.post(props.previewUrl, requestBody())
    preview.value = data
    if (!resolved.value) resolved.value = data.repo_branch ?? ''
    if (branch.value !== (data.repo_branch ?? '')) {
      adopting = true
      branch.value = data.repo_branch ?? ''
      await nextTick()
      adopting = false
    }
  } catch (e: any) {
    preview.value = null
    error.value = e.response?.data?.detail ?? e.message ?? 'preview failed'
  } finally {
    loading.value = false
  }
}

watch(
  () => props.modelValue,
  (isOpen) => {
    if (isOpen) {
      applyRemovals.value = false
      promoteImage.value = false
      promoteModelPath.value = false
      promoteGpus.value = false
      showDescription.value = false
      branch.value = ''
      resolved.value = ''
      load()
    }
  },
)
watch(branch, () => {
  if (!adopting && props.modelValue && !loading.value) load()
})
watch([applyRemovals, promoteImage, promoteModelPath, promoteGpus], () => {
  if (props.modelValue && !loading.value) load()
})

/** The "also update X" ticks are offered only when X actually differs and the
 *  policy did not already say platform — i.e. when it shows up as reported. */
const reportedKeys = computed(() => new Set((preview.value?.plan.reported ?? []).map((r) => r.key)))

const changeRows = computed(() => {
  const plan = preview.value?.plan
  if (!plan) return []
  const rows = plan.changes.map((c) => ({
    key: c.key, flag: c.flag || c.key, before: c.before, after: c.after, kind: c.kind,
  }))
  if (plan.gpus) rows.push({ key: 'gpus', flag: t('promotion.gpusPerReplica'), before: plan.gpus.before, after: plan.gpus.after, kind: 'changed' })
  if (plan.image_tag) rows.push({ key: 'image', flag: t('promotion.imageTag'), before: plan.image_tag.before, after: plan.image_tag.after, kind: 'changed' })
  if (plan.model_path) rows.push({ key: 'model_path', flag: t('promotion.modelPath'), before: plan.model_path.before, after: plan.model_path.after, kind: 'changed' })
  return rows
})

function fmt(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (value === true) return 'on'
  if (value === false) return 'off'
  return String(value)
}

const evidenceLines = computed(() => {
  const ev = preview.value?.evidence ?? {}
  const lines: string[] = []
  if (ev.target_metric) {
    let line = `${ev.target_metric}: ${ev.score ?? '—'}`
    if (typeof ev.vs_baseline === 'number') line += ` — ${t('promotion.vsBaseline', { x: (ev.vs_baseline as number).toFixed(3) })}`
    lines.push(line)
  }
  if (ev.benchmark_slug) lines.push(`benchmark: ${ev.benchmark_slug}${ev.stage ? ` (${ev.stage})` : ''}`)
  if (ev.dataset_build_id) lines.push(`dataset build: ${ev.dataset_build_id}`)
  if (ev.image_ref) lines.push(`image: ${ev.image_ref}`)
  if (ev.card_type) lines.push(`card: ${ev.card_type}`)
  return lines
})

async function promote() {
  busy.value = true
  try {
    const { data } = await api.post(props.promoteUrl, { ...requestBody(), target: 'gitlab' })
    result.value = data
    if (data.state === 'failed') {
      ElMessage.error(`${t('promotion.failed')}: ${data.error}`)
    } else {
      ElMessage.success(data.refs?.dry_run ? t('promotion.recorded') : t('promotion.opened'))
      emit('promoted', data)
    }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail ?? t('promotion.failed'))
  } finally {
    busy.value = false
  }
}

function mrUrl(p: Promotion | null): string {
  return (p?.refs?.mr_url as string) ?? ''
}
</script>

<template>
  <el-dialog v-model="open" :title="t('promotion.title')" width="860px" top="4vh" class="mr-dialog">
    <div v-if="loading" class="muted">{{ t('promotion.loading') }}</div>
    <el-alert v-else-if="error" type="error" :closable="false" :title="error" show-icon />

    <template v-else-if="preview">
      <p class="muted subtitle">
        {{ t('promotion.subtitle', { path: preview.repo_path || '—', branch: preview.repo_branch || '—' }) }}
      </p>

      <div v-if="preview.repo_project" class="branch-row">
        <span class="muted">{{ t('promotion.targetBranch') }}</span>
        <DeployBranchSelect v-model="branch" :project="preview.repo_project" size="small"
          :clearable="false" class="branch-pick" />
      </div>
      <div v-if="preview.tracked_branch && preview.tracked_branch !== preview.repo_branch"
        class="muted tiny branch-note">
        {{ t('promotion.branchNotTracked', { branch: preview.tracked_branch }) }}
      </div>

      <el-alert v-if="!preview.ready" type="info" :closable="false" show-icon
        :title="preview.reason || t('promotion.nothingToChange')">
        <template v-if="!preview.baseline_id || (preview.reason && preview.reason.includes('not bound'))">
          <el-button size="small" link type="primary" @click="router.push('/baselines')">
            {{ t('promotion.goBaselines') }}
          </el-button>
        </template>
      </el-alert>

      <template v-else>
        <el-alert v-if="preview.offline" type="warning" :closable="false" show-icon
          :title="t('promotion.offline')" class="note" />
        <el-alert v-if="preview.stale" type="warning" :closable="false" show-icon class="note"
          :title="t('promotion.stale', { synced: preview.synced_commit.slice(0, 10), head: preview.head_commit.slice(0, 10) })">
          <div v-if="preview.drift.length" class="tiny">
            {{ t('promotion.drift') }}
            <span v-for="d in preview.drift" :key="d.key" class="mono drift">
              {{ d.key }} {{ fmt(d.before) }} → {{ fmt(d.after) }}
            </span>
          </div>
        </el-alert>
        <el-alert v-if="preview.unresolved" type="warning" :closable="false" show-icon class="note"
          :title="t('promotion.unresolved', { n: preview.unresolved })">
          <el-button size="small" link type="primary" @click="router.push('/baselines')">
            {{ t('promotion.goBaselines') }}
          </el-button>
        </el-alert>

        <h4>{{ t('promotion.changes') }}</h4>
        <el-table :data="changeRows" size="small" class="changes">
          <el-table-column :label="t('promotion.knob')" min-width="220">
            <template #default="{ row }"><span class="mono">{{ row.flag }}</span></template>
          </el-table-column>
          <el-table-column :label="t('promotion.production')" min-width="160">
            <template #default="{ row }"><span class="mono muted">{{ fmt(row.before) }}</span></template>
          </el-table-column>
          <el-table-column :label="t('promotion.tuned')" min-width="160">
            <template #default="{ row }">
              <span class="mono" :class="row.kind === 'removed' || row.after === false ? 'removed' : 'added'">
                {{ fmt(row.after) }}</span>
            </template>
          </el-table-column>
        </el-table>

        <div class="ticks">
          <el-checkbox v-if="preview.plan.kept.length" v-model="applyRemovals">
            {{ t('promotion.applyRemovals') }} ({{ preview.plan.kept.length }})
          </el-checkbox>
          <el-checkbox v-if="reportedKeys.has('image')" v-model="promoteImage">{{ t('promotion.promoteImage') }}</el-checkbox>
          <el-checkbox v-if="reportedKeys.has('model_path')" v-model="promoteModelPath">{{ t('promotion.promoteModelPath') }}</el-checkbox>
          <el-checkbox v-if="reportedKeys.has('gpus')" v-model="promoteGpus">{{ t('promotion.promoteGpus') }}</el-checkbox>
        </div>

        <template v-if="preview.plan.kept.length || preview.plan.reported.length || preview.plan.warnings.length">
          <h4>{{ t('promotion.notApplied') }}</h4>
          <ul class="caveats">
            <li v-for="k in preview.plan.kept" :key="'k' + k.key">
              {{ t('promotion.kept', { flag: `${k.flag || k.key}=${fmt(k.before)}` }) }}
            </li>
            <li v-for="r in preview.plan.reported" :key="'r' + r.key">
              {{ t('promotion.reported', { flag: r.flag || r.key, before: fmt(r.before), after: fmt(r.after), note: r.note }) }}
            </li>
            <li v-for="(w, i) in preview.plan.warnings" :key="'w' + i">{{ w }}</li>
          </ul>
        </template>
        <details v-if="preview.plan.ignored.length" class="ignored">
          <summary class="muted tiny">{{ t('promotion.ignored', { n: preview.plan.ignored.length }) }}</summary>
          <ul class="caveats tiny">
            <li v-for="i in preview.plan.ignored" :key="'i' + i.key">
              {{ t('promotion.ignoredRow', { flag: i.flag || i.key, before: fmt(i.before), after: fmt(i.after) }) }}
            </li>
          </ul>
        </details>

        <template v-if="evidenceLines.length">
          <h4>{{ t('promotion.evidence') }}</h4>
          <ul class="caveats">
            <li v-for="(line, i) in evidenceLines" :key="i">{{ line }}</li>
          </ul>
        </template>

        <h4>{{ t('promotion.diff') }}
          <el-button size="small" link @click="copyText(preview!.diff, 'Diff copied')">copy</el-button>
        </h4>
        <pre class="diff"><template v-for="(line, i) in preview.diff.split('\n')" :key="i"><span
          :class="line.startsWith('+') && !line.startsWith('+++') ? 'added' : line.startsWith('-') && !line.startsWith('---') ? 'removed' : ''">{{ line }}</span>
</template></pre>

        <details class="ignored" :open="showDescription">
          <summary class="muted tiny">{{ t('promotion.description') }} · {{ t('promotion.branch') }}
            <span class="mono">{{ preview.source_branch }}</span></summary>
          <pre class="description">{{ preview.description }}</pre>
        </details>

        <el-alert v-if="preview.dry_run" type="info" :closable="false" :title="t('promotion.dryRun')" class="note" />
      </template>
    </template>

    <el-alert v-if="result && result.state !== 'failed'" type="success" :closable="false" show-icon class="note"
      :title="result.refs?.dry_run ? t('promotion.recorded') : t('promotion.opened')">
      <a v-if="mrUrl(result)" :href="mrUrl(result)" target="_blank" rel="noopener">{{ t('promotion.viewMr') }}</a>
      <span v-else class="mono tiny">{{ result.refs?.branch }}</span>
    </el-alert>

    <template #footer>
      <el-button @click="open = false">{{ t('common.close') }}</el-button>
      <el-button type="primary" :disabled="!preview?.ready || !!result || preview?.offline" :loading="busy" @click="promote">
        {{ preview?.dry_run ? t('promotion.recordOnly') : t('promotion.open') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.subtitle { margin: 0 0 10px; }
.branch-row { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
.branch-pick { max-width: 420px; }
.branch-note { margin-bottom: 8px; }
h4 { margin: 14px 0 6px; font-size: 13px; }
.note { margin-bottom: 8px; }
.ticks { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 8px; }
.caveats { margin: 0; padding-left: 18px; font-size: 12.5px; }
.caveats li { margin: 2px 0; }
.ignored { margin-top: 8px; }
.diff, .description {
  max-height: 320px; overflow: auto; font-size: 12px; line-height: 1.45;
  background: var(--el-fill-color-light); padding: 10px; border-radius: 6px; margin: 0;
  white-space: pre;
}
.description { white-space: pre-wrap; }
.added { color: var(--el-color-success); }
.removed { color: var(--el-color-danger); }
.diff .added { background: color-mix(in srgb, var(--el-color-success) 12%, transparent); display: inline-block; width: 100%; }
.diff .removed { background: color-mix(in srgb, var(--el-color-danger) 12%, transparent); display: inline-block; width: 100%; }
.drift { margin-left: 8px; }
.tiny { font-size: 11.5px; }
</style>
