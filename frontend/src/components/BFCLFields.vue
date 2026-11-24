<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { BFCLConfig } from '../utils/workload'
import { useI18n } from '../i18n'
const spec = defineModel<BFCLConfig>({ required: true })
const { t } = useI18n()
watch(() => spec.value.task_set, (task) => {
  if (task === 'bfcl-v4-smoke-v1') spec.value.cases_per_category = 1
})
const gridText = ref((spec.value.concurrency_grid ?? []).join(', '))
const grid = computed(() => {
  const parts = gridText.value.trim().split(/[,\s]+/).filter(Boolean)
  return parts.length ? parts.map(Number) : null
})
watch(grid, (value) => { spec.value.concurrency_grid = value })
const gridBad = computed(() => !!grid.value && (grid.value.length < 2 || grid.value.length > 16
  || grid.value.some((c, i) => !Number.isInteger(c) || c < 1 || c > 128 || (i > 0 && c <= grid.value![i - 1]))))
const taskSets = ['bfcl-v4-tool-use-v1', 'bfcl-v4-multi-turn-long-context-v1', 'bfcl-v4-smoke-v1']
</script>

<template>
  <div class="bfcl-fields">
    <label class="field">
      <span class="muted tiny">{{ t('bfcl.taskSet') }}</span>
      <el-select v-model="spec.task_set" size="small">
        <el-option v-for="task in taskSets" :key="task" :value="task" :label="task" />
      </el-select>
    </label>
    <p class="muted tiny">{{ t('bfcl.note') }}</p>
    <label class="field">
      <span class="muted tiny">{{ t('bfcl.grid') }}</span>
      <el-input v-model="gridText" placeholder="1, 3, 5, 6, 8" size="small" />
      <span v-if="gridBad" class="tiny">{{ t('bfcl.gridInvalid') }}</span>
    </label>
    <div class="bfcl-grid">
      <label v-for="field in [
        { key: 'cases_per_category' as const, label: 'cases', min: 1, max: 200 },
        { key: 'concurrency' as const, label: 'concurrency', min: 1, max: 128 },
        { key: 'max_seconds' as const, label: 'maxSeconds', min: 1, max: 86400 },
        { key: 'request_timeout_seconds' as const, label: 'timeout', min: 1, max: 3600 },
        { key: 'request_retries' as const, label: 'retries', min: 0, max: 3 },
        { key: 'min_ttft_coverage' as const, label: 'ttftCoverage', min: 0, max: 1 },
        { key: 'min_usage_coverage' as const, label: 'usageCoverage', min: 0, max: 1 },
        { key: 'min_cache_usage_coverage' as const, label: 'cacheCoverage', min: 0, max: 1 },
      ]" :key="field.key" class="field">
        <span class="muted tiny">{{ t(`bfcl.${field.label}`) }}</span>
        <el-input-number v-model="spec[field.key]" :min="field.min" :max="field.max"
          :step="field.max === 1 ? 0.1 : 1" size="small" controls-position="right"
          :disabled="(field.key === 'cases_per_category' && spec.task_set === 'bfcl-v4-smoke-v1')
            || (field.key === 'concurrency' && !!grid?.length)" />
      </label>
    </div>
    <label class="field">
      <span class="muted tiny">{{ t('bfcl.requiredMetrics') }}</span>
      <el-select v-model="spec.required_metrics" multiple filterable allow-create size="small">
        <el-option v-for="metric in ['accuracy', 'passed_cases_per_hour', 'ttft_p90_ms', 'total_tpm', 'cache_hit_rate']"
          :key="metric" :value="metric" :label="metric" />
      </el-select>
    </label>
    <el-checkbox v-model="spec.probe_endpoint" size="small">{{ t('bfcl.probe') }}</el-checkbox>
  </div>
</template>

<style scoped>
.bfcl-fields { display: grid; gap: 10px; width: 100%; }
.bfcl-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.field { display: grid; gap: 5px; }
@media (max-width: 540px) { .bfcl-grid { grid-template-columns: 1fr; } }
</style>
