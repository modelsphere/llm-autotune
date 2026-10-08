<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { api } from '@/plugins/api'

defineProps<{ me: unknown; reload: () => unknown }>()
const greeting = ref('')

onMounted(async () => {
  greeting.value = (await api.get('/example/settings/greeting')).data.value
})
</script>

<template>
  <el-card>
    <b>Example plugin</b>
    <p class="muted">{{ greeting }}</p>
  </el-card>
</template>
