<script setup lang="ts">
/** Where a cluster is relative to AutoTune: its own cluster (runs can be
 *  reached by Service name or NodePort), an external one (NodePort only), or
 *  unknown (no CA to compare). Told apart by the cluster's CA. */
import { computed } from 'vue'
import { useI18n } from '../i18n'

const props = defineProps<{ same: boolean | null | undefined; detecting?: boolean }>()
const { t } = useI18n()

const look = computed(() => {
  if (props.detecting) return { type: 'info', text: t('resources.loc.detecting'), tip: '' }
  if (props.same === true) {
    return { type: 'success', text: t('resources.loc.internal'), tip: t('resources.loc.internalTip') }
  }
  if (props.same === false) {
    return { type: 'warning', text: t('resources.loc.external'), tip: t('resources.loc.externalTip') }
  }
  return { type: 'info', text: t('resources.loc.unknown'), tip: t('resources.loc.unknownTip') }
})
</script>

<template>
  <el-tooltip :content="look.tip" :disabled="!look.tip" placement="top">
    <el-tag size="small" effect="plain" :type="(look.type as any)" class="location">
      {{ look.text }}
    </el-tag>
  </el-tooltip>
</template>

<style scoped>
.location {
  cursor: default;
}
</style>
