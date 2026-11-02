<script setup lang="ts">
/** A cluster's last probe as a row of checks — reachable, nodes, operator,
 *  card types — each an icon and a word, its detail on hover. */
import { computed } from 'vue'
import { CircleCheckFilled, CircleCloseFilled, QuestionFilled, WarningFilled } from '@element-plus/icons-vue'
import { useI18n } from '../i18n'

const props = defineProps<{
  probe: Record<string, any> | null | undefined
  workloadKind: string
}>()
const { t } = useI18n()

type Check = { key: string; state: 'ok' | 'warn' | 'bad' | 'unknown'; label: string; tip: string }

const warningsAbout = (word: string) =>
  ((props.probe?.warnings ?? []) as string[]).filter((w) => w.toLowerCase().includes(word)).join('\n')

const checks = computed<Check[]>(() => {
  const p = props.probe
  if (!p || !Object.keys(p).length) {
    return [{ key: 'none', state: 'unknown', label: t('resources.health.notProbed'), tip: '' }]
  }
  const out: Check[] = [{
    key: 'api', state: p.reachable ? 'ok' : 'bad',
    label: t('resources.health.api'),
    tip: p.reachable ? '' : ((p.warnings ?? []) as string[]).join('\n'),
  }]
  if (p.reachable) {
    out.push({
      key: 'nodes', state: p.nodes_readable ? 'ok' : 'warn',
      label: p.nodes_readable
        ? t('resources.health.nodes', { n: p.node_count ?? 0 })
        : t('resources.health.noNodes'),
      tip: p.nodes_readable ? '' : warningsAbout('node'),
    })
    if (props.workloadKind !== 'deployment') {
      out.push({
        key: 'operator',
        state: p.crd_present === true ? 'ok' : p.crd_present === false ? 'bad' : 'unknown',
        label: t('resources.health.operator'),
        tip: p.crd_present === false ? warningsAbout('cannot be listed') || warningsAbout('installed') : '',
      })
    }
  }
  return out
})

const icon = { ok: CircleCheckFilled, warn: WarningFilled, bad: CircleCloseFilled, unknown: QuestionFilled }
</script>

<template>
  <span class="health">
    <el-tooltip v-for="c in checks" :key="c.key" :content="c.tip" :disabled="!c.tip"
      placement="top" :show-after="200">
      <span class="check" :class="c.state">
        <el-icon :size="13"><component :is="icon[c.state]" /></el-icon>
        {{ c.label }}
      </span>
    </el-tooltip>
    <span v-for="g in (probe?.gpu_types ?? [])" :key="g" class="check gpu">{{ g }}</span>
  </span>
</template>

<style scoped>
.health {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  align-items: center;
}
.check {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.check.ok .el-icon { color: var(--el-color-success); }
.check.warn .el-icon { color: var(--el-color-warning); }
.check.bad { color: var(--el-color-danger); }
.check.bad .el-icon { color: var(--el-color-danger); }
.check.unknown .el-icon { color: var(--el-text-color-placeholder); }
.check.gpu {
  font-family: var(--el-font-family-mono, monospace);
  font-size: 11.5px;
}
</style>
