<script setup lang="ts">
/** "Which release branch?" — asked in four places, answered the same way.
 *
 *  The deploy repo keeps one release branch per model × card × engine
 *  (`release/kimi-k3-nvidia_b300-sglang`, …), so a merge request has to name
 *  one and nobody should have to remember the spelling. This offers what the
 *  repo actually has; when GitLab is not configured or not reachable it stays
 *  an ordinary text field rather than blocking, because a branch typed by hand
 *  is still a valid answer — it is only checked when the file is read.
 */
import { onMounted, ref, watch } from 'vue'
import { api, type RepoBranch } from '../api/client'
import { useI18n } from '../i18n'

const props = withDefaults(
  defineProps<{
    modelValue: string
    /** Which repo to list. Empty = the platform's configured default. */
    project?: string
    /** GitLab's own filter; `^release/` is a prefix match. */
    search?: string
    placeholder?: string
    size?: 'small' | 'default' | 'large'
    clearable?: boolean
  }>(),
  { project: '', search: '^release/', placeholder: '', size: 'default', clearable: true },
)
const emit = defineEmits<{ (e: 'update:modelValue', branch: string): void }>()

const { t } = useI18n()
const branches = ref<RepoBranch[]>([])
const error = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const { data } = await api.get('/baselines/branches', {
      params: { project: props.project, search: props.search },
    })
    branches.value = data.branches ?? []
    error.value = data.error ?? ''
  } catch (e: any) {
    branches.value = []
    error.value = e.response?.data?.detail ?? e.message ?? 'could not list branches'
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.project, load)
</script>

<template>
  <div class="branch-select">
    <el-select
      :model-value="modelValue"
      filterable
      allow-create
      default-first-option
      :clearable="clearable"
      :loading="loading"
      :size="size"
      :placeholder="placeholder || t('binding.branchPlaceholder')"
      class="mono full"
      @update:model-value="(v: string) => emit('update:modelValue', v ?? '')"
    >
      <el-option v-for="b in branches" :key="b.name" :label="b.name" :value="b.name">
        <span class="mono">{{ b.name }}</span>
        <span class="muted tiny commit">{{ b.commit }}</span>
      </el-option>
    </el-select>
    <div v-if="error" class="muted tiny hint">{{ t('binding.branchesUnavailable') }}</div>
  </div>
</template>

<style scoped>
.branch-select { width: 100%; }
.full { width: 100%; }
.commit { float: right; margin-left: 12px; }
.hint { margin-top: 2px; }
.tiny { font-size: 11.5px; }
</style>
