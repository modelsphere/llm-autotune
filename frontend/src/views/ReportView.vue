<script setup lang="ts">
/** One saved report, rendered by the same renderer the HTML export carries
 *  (src/report/): the agent's markdown with its chart/table/command blocks
 *  drawn live from the comparison frozen with the report. Older reports with
 *  PNG assets render too — their images are fetched through the API so the
 *  session's credentials apply. */
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, type AgentReportDetail } from '../api/client'
import { useI18n } from '../i18n'
import type { Comparison } from '../report/data'
import { renderReport } from '../report/render'
import { exactTime } from '../utils/time'

const LANG_NAMES: Record<string, string> = { en: 'English', zh: '中文' }

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const report = ref<AgentReportDetail | null>(null)
const missing = ref(false)
const exporting = ref(false)
const body = ref<HTMLElement | null>(null)
let assetUrls: string[] = []
let dispose: (() => void) | null = null

function cleanup() {
  dispose?.()
  dispose = null
  for (const url of assetUrls) URL.revokeObjectURL(url)
  assetUrls = []
}

async function load() {
  cleanup()
  report.value = null
  missing.value = false
  const id = route.params.id
  let detail: AgentReportDetail
  try {
    detail = (await api.get(`/agent/v1/reports/${id}`)).data
  } catch {
    missing.value = true
    return
  }
  // An <img> cannot send the auth header: fetch each asset, hand over blob URLs.
  const urls: Record<string, string> = {}
  await Promise.all(detail.asset_names.map(async (name) => {
    try {
      const { data } = await api.get(`/agent/v1/reports/${id}/assets/${name}`, { responseType: 'blob' })
      urls[name] = URL.createObjectURL(data)
    } catch {
      /* a missing image leaves a broken image, not a broken page */
    }
  }))
  assetUrls = Object.values(urls)
  report.value = detail
  await nextTick()
  if (!body.value) return
  dispose = renderReport(body.value, {
    markdown: detail.markdown,
    comparison: detail.comparison as unknown as Comparison,
    lang: detail.lang === 'zh' ? 'zh' : 'en',
    labels: detail.labels,
    resolveAsset: (name) => urls[name] ?? name,
  })
}

const translations = computed(() =>
  (report.value?.translations ?? []).length > 1 ? report.value!.translations : [])

function switchTo(id: number) {
  if (id !== report.value?.id) router.push(`/reports/${id}`)
}

function slug(): string {
  const title = report.value?.title ?? 'report'
  return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'report'
}

async function save(path: string, name: string): Promise<void> {
  const { data } = await api.get(path, { responseType: 'blob' })
  const url = URL.createObjectURL(data)
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  URL.revokeObjectURL(url)
}

const downloading = ref(false)

async function downloadSource() {
  if (!report.value) return
  downloading.value = true
  try {
    await save(`/agent/v1/reports/${report.value.id}/bundle.zip`, `${slug()}-${report.value.id}.zip`)
  } catch {
    ElMessage.error(t('reports.exportFailed'))
  } finally {
    downloading.value = false
  }
}

async function exportHtml() {
  if (!report.value) return
  exporting.value = true
  try {
    await save(`/agent/v1/reports/${report.value.id}/export.html`, `${slug()}-${report.value.id}.html`)
    ElMessage.success(t('reports.exported'))
  } catch {
    ElMessage.error(t('reports.exportFailed'))
  } finally {
    exporting.value = false
  }
}

const generator = computed(() => {
  const g = report.value?.generator ?? {}
  return Object.entries(g).map(([k, v]) => `${k}: ${String(v)}`).join(' · ')
})

watch(() => route.params.id, load, { immediate: true })
onBeforeUnmount(cleanup)
</script>

<template>
  <div class="page report-page">
    <div class="header-row">
      <div>
        <router-link to="/reports" class="muted tiny">← {{ t('reports.back') }}</router-link>
        <span v-if="report" class="muted tiny meta">
          <template v-if="report.campaign_id">
            {{ t('reports.campaign') }}:
            <router-link :to="`/campaigns/${report.campaign_id}`">
              #{{ report.campaign_id }}
            </router-link>
            ·
          </template>
          {{ t('reports.inputs') }}:
          <span class="mono">run {{ report.baseline_run_id }}</span>
          →
          <span class="mono">{{ report.attempt_run_ids.map((id) => `run ${id}`).join(', ') }}</span>
          <template v-if="report.created_by_name"> · {{ t('reports.by') }} {{ report.created_by_name }}</template>
          <template v-if="report.created_at"> · {{ exactTime(report.created_at) }}</template>
          <template v-if="generator"> · {{ t('reports.generator') }} {{ generator }}</template>
          <el-tag v-if="!report.comparable" size="small" type="warning" class="tag"
            :title="t('reports.notComparableHint')">
            {{ t('reports.notComparable') }}
          </el-tag>
        </span>
      </div>
      <div v-if="report" class="actions">
        <el-radio-group v-if="translations.length" :model-value="report.id" size="small"
          :aria-label="t('reports.language')" @change="(v: string | number | boolean | undefined) => switchTo(Number(v))">
          <el-radio-button v-for="tr in translations" :key="tr.id" :value="tr.id">
            {{ LANG_NAMES[tr.lang] ?? tr.lang }}
          </el-radio-button>
        </el-radio-group>
        <el-button type="primary" :loading="exporting" @click="exportHtml">{{ t('reports.exportHtml') }}</el-button>
        <el-button :loading="downloading" :title="t('reports.downloadHint')" @click="downloadSource">
          {{ t('reports.download') }}
        </el-button>
      </div>
    </div>

    <el-alert v-if="report && !report.comparable" type="warning" :closable="false" show-icon
      :title="t('reports.notComparableHint')" class="warn" />

    <p v-if="missing" class="muted">{{ t('reports.missing') }}</p>
    <p v-else-if="!report" class="muted">{{ t('reports.loading') }}</p>
    <div v-show="report" ref="body" class="report-body" />
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
.actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.report-page { max-width: 1100px; }
.meta { display: block; margin-top: 4px; }
.tag { margin-left: 8px; }
.warn { margin: 12px 0; }
.report-body { background: #fff; border-radius: 8px; padding: 12px 24px 40px; }
</style>
