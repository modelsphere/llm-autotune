<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { api, useI18n } from '@/plugins/api'

const { t } = useI18n()
const ticks = ref<{ ticks: number; active_campaigns: number | null } | null>(null)

onMounted(async () => {
  ticks.value = (await api.get('/example/ticks')).data
})
</script>

<template>
  <div class="page">
    <h1 class="page-title">{{ t('example.nav') }}</h1>
    <p v-if="ticks">{{ t('example.ticks') }}: {{ ticks.ticks }}</p>
  </div>
</template>
