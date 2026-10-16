<script setup lang="ts">
/** What a page shows before its data is there: a spinner while it loads, or
 *  what went wrong and a way to try again. The spinner waits a moment before
 *  it appears, so a page that loads at once does not flicker. */
import { onBeforeUnmount, ref } from 'vue'
import { useI18n } from '../i18n'

defineProps<{ error?: string }>()
defineEmits<{ retry: [] }>()
const { t } = useI18n()

const shown = ref(false)
const timer = window.setTimeout(() => (shown.value = true), 150)
onBeforeUnmount(() => window.clearTimeout(timer))
</script>

<template>
  <div class="page-loading">
    <template v-if="error">
      <div class="failed">{{ t('common.loadFailed') }}</div>
      <div class="muted detail">{{ error }}</div>
      <el-button size="small" @click="$emit('retry')">{{ t('common.retry') }}</el-button>
    </template>
    <template v-else-if="shown">
      <div class="spin" />
      <div class="muted">{{ t('common.loading') }}</div>
    </template>
  </div>
</template>

<style scoped>
.page-loading {
  min-height: 240px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  font-size: 13px;
}
.muted {
  color: var(--autotune-muted);
}
.failed {
  font-weight: 600;
}
.detail {
  max-width: 560px;
  text-align: center;
}
.spin {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 3px solid var(--autotune-border);
  border-top-color: var(--el-color-primary);
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
