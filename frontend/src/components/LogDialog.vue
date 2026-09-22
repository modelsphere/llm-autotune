<script setup lang="ts">
/** A log viewer dialog. Pick one of several logs (the policy container, or any
 *  run's engine container), read it, refresh it, or download it — so debugging a
 *  campaign needs no page-jumping. `docker logs` merges
 *  stdout and stderr, so each source is the container's full output. */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { downloadText, type LogSource } from '../api/client'
import { useI18n } from '../i18n'

const props = defineProps<{
  modelValue: boolean
  title: string
  sources: LogSource[]
}>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
const { t } = useI18n()

const selected = ref('')
const text = ref('')
const loading = ref(false)

function current(): LogSource | undefined {
  return props.sources.find((s) => s.key === selected.value)
}

async function load() {
  const src = current()
  if (!src) {
    text.value = ''
    return
  }
  loading.value = true
  try {
    text.value = await src.fetch()
  } catch {
    text.value = ''
    ElMessage.error('Could not load the log')
  } finally {
    loading.value = false
  }
}

function download() {
  const src = current()
  if (src) downloadText(src.filename, text.value)
}

// On open — or when the source list changes — default to the first source and load.
watch(
  () => [props.modelValue, props.sources.map((s) => s.key).join(',')],
  () => {
    if (!props.modelValue) return
    if (!current()) selected.value = props.sources[0]?.key ?? ''
    void load()
  },
  { immediate: true },
)
watch(selected, load)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="860px"
    top="6vh"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="log-bar">
      <el-select v-if="sources.length > 1" v-model="selected" size="small" class="log-pick">
        <el-option v-for="s in sources" :key="s.key" :label="s.label" :value="s.key" />
      </el-select>
      <span v-else class="muted tiny mono">{{ sources[0]?.label }}</span>
      <span class="spacer" />
      <el-button size="small" :loading="loading" @click="load">{{ t('common.refresh') }}</el-button>
      <el-button size="small" :disabled="!text" @click="download">
        {{ t('common.download') }}
      </el-button>
    </div>
    <pre v-loading="loading" class="mono log-body">{{ text || '(empty)' }}</pre>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.log-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.log-bar .spacer {
  flex: 1;
}
.log-pick {
  width: 340px;
}
.log-body {
  margin: 0;
  max-height: 62vh;
  overflow: auto;
  padding: 12px;
  background: var(--autotune-bg, #f5f7fa);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}
.muted {
  color: var(--autotune-muted, #909399);
}
.tiny {
  font-size: 12px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
