<script setup lang="ts">
/** A list's filters as one bar: a text search, the active filters as chips
 *  ("Status: active, paused ×"), and one Filter button that opens every field
 *  (status, owner, …) with its values and how many rows have each. The choice
 *  lives in the page's URL, so a filtered list can be reloaded or sent to
 *  someone as a link. */
import { Close, Filter, Search } from '@element-plus/icons-vue'
import { computed, ref, watch } from 'vue'
import { useI18n } from '../i18n'

export interface FilterOption {
  value: string
  label: string
  count?: number
}
export interface FilterField {
  key: string
  label: string
  options: FilterOption[]
  /** Unused since the filters became one bar; kept for callers that set it. */
  width?: number
}

const props = defineProps<{
  fields: FilterField[]
  /** Selected values by field key. */
  modelValue: Record<string, string[]>
  /** Shown when set (including ''), as the placeholder of a text search. */
  searchPlaceholder?: string
  search?: string
  /** "12 of 40", when the parent knows both. */
  shown?: number
  total?: number
}>()
const emit = defineEmits<{
  'update:modelValue': [Record<string, string[]>]
  'update:search': [string]
}>()
const { t } = useI18n()

const values = (key: string) => props.modelValue[key] ?? []
function set(key: string, next: string[]) {
  emit('update:modelValue', { ...props.modelValue, [key]: next })
}
function toggle(key: string, value: string) {
  const now = values(key)
  set(key, now.includes(value) ? now.filter((v) => v !== value) : [...now, value])
}

/** One chip per field with something ticked, its values by label. */
const chips = computed(() => props.fields
  .filter((f) => values(f.key).length)
  .map((f) => ({
    key: f.key,
    label: f.label,
    text: values(f.key).map((v) => f.options.find((o) => o.value === v)?.label ?? v).join(', '),
  })))
const count = computed(() => Object.values(props.modelValue).reduce((n, v) => n + (v?.length ?? 0), 0))
const active = computed(() => !!props.search?.trim() || count.value > 0)

function clear() {
  emit('update:modelValue', Object.fromEntries(props.fields.map((f) => [f.key, []])))
  if (props.search !== undefined) emit('update:search', '')
}

// -- the picker: fields on the left, the chosen field's values on the right ---
const open = ref(false)
const field = ref('')
const find = ref('')
watch(open, (isOpen) => {
  if (isOpen && !props.fields.some((f) => f.key === field.value)) field.value = props.fields[0]?.key ?? ''
  find.value = ''
})
watch(field, () => (find.value = ''))
const current = computed(() => props.fields.find((f) => f.key === field.value))
const options = computed(() => {
  const text = find.value.trim().toLowerCase()
  return (current.value?.options ?? []).filter((o) => !text || o.label.toLowerCase().includes(text))
})
</script>

<template>
  <div class="list-filters">
    <div class="bar" :class="{ focused: open }">
      <el-icon class="lead"><Search /></el-icon>
      <span v-for="c in chips" :key="c.key" class="chip">
        <span class="chip-key">{{ c.label }}:</span>
        <span class="chip-val" :title="c.text">{{ c.text }}</span>
        <button type="button" class="chip-x" :aria-label="t('filters.remove', { name: c.label })"
          @click="set(c.key, [])"><el-icon><Close /></el-icon></button>
      </span>
      <input v-if="searchPlaceholder !== undefined" class="text" :value="search"
        :placeholder="chips.length ? '' : searchPlaceholder" :aria-label="searchPlaceholder"
        @input="emit('update:search', ($event.target as HTMLInputElement).value)" />
      <span v-else class="text placeholder">{{ chips.length ? '' : t('filters.hint') }}</span>
      <button v-if="active" type="button" class="clear" @click="clear">{{ t('filters.clear') }}</button>
      <el-popover v-if="fields.length" v-model:visible="open" trigger="click" placement="bottom-end"
        :width="440" :show-arrow="false">
        <template #reference>
          <button type="button" class="filter-btn" :class="{ on: count }">
            <el-icon><Filter /></el-icon>
            <span>{{ t('filters.filter') }}</span>
            <span v-if="count" class="badge">{{ count }}</span>
          </button>
        </template>
        <div class="picker">
          <ul class="fields">
            <li v-for="f in fields" :key="f.key" :class="{ on: f.key === field }" @click="field = f.key">
              <span>{{ f.label }}</span>
              <span v-if="values(f.key).length" class="badge">{{ values(f.key).length }}</span>
            </li>
          </ul>
          <div class="values">
            <el-input v-if="(current?.options.length ?? 0) > 8" v-model="find" size="small" clearable
              :placeholder="t('filters.find')" :prefix-icon="Search" class="find" />
            <div class="opts">
              <label v-for="o in options" :key="o.value" class="opt">
                <el-checkbox :model-value="values(field).includes(o.value)"
                  @change="toggle(field, o.value)" />
                <span class="opt-label">{{ o.label }}</span>
                <span v-if="o.count != null" class="opt-count">{{ o.count }}</span>
              </label>
              <div v-if="!options.length" class="none">{{ t('filters.noValues') }}</div>
            </div>
          </div>
        </div>
      </el-popover>
    </div>
    <span v-if="active && shown != null && total != null" class="shown">
      {{ t('filters.shown', { shown, total }) }}
    </span>
  </div>
</template>

<style scoped>
.list-filters { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.bar {
  flex: 1; display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  min-height: 34px; padding: 3px 4px 3px 10px; box-sizing: border-box;
  background: var(--autotune-surface); border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base); transition: border-color 0.15s, box-shadow 0.15s;
}
.bar:focus-within, .bar.focused {
  border-color: var(--el-color-primary); box-shadow: 0 0 0 1px var(--el-color-primary-light-7);
}
.lead { color: var(--el-text-color-placeholder); }
.text {
  flex: 1; min-width: 140px; border: none; outline: none; background: transparent;
  font: inherit; font-size: 13px; color: var(--autotune-text); padding: 4px 2px;
}
.text::placeholder, .placeholder { color: var(--el-text-color-placeholder); }
.chip {
  display: inline-flex; align-items: center; gap: 4px; max-width: 320px; height: 24px;
  padding: 0 4px 0 8px; font-size: 12px; border-radius: 12px;
  background: var(--el-color-primary-light-9); border: 1px solid var(--el-color-primary-light-7);
  color: var(--el-color-primary-dark-2);
}
.chip-key { color: var(--autotune-muted); }
.chip-val { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
button { font: inherit; cursor: pointer; }
.chip-x {
  display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px;
  padding: 0; border: none; border-radius: 50%; background: transparent; color: var(--autotune-muted);
}
.chip-x:hover { background: var(--el-color-primary-light-7); color: var(--el-color-primary-dark-2); }
.clear { border: none; background: none; font-size: 12px; color: var(--autotune-muted); padding: 4px 6px; }
.clear:hover { color: var(--el-color-primary); }
.filter-btn {
  display: inline-flex; align-items: center; gap: 5px; height: 26px; padding: 0 10px;
  font-size: 13px; color: var(--autotune-text); background: var(--autotune-bg);
  border: 1px solid var(--autotune-border); border-radius: 4px;
}
.filter-btn:hover, .filter-btn.on {
  color: var(--el-color-primary); border-color: var(--el-color-primary-light-7);
  background: var(--el-color-primary-light-9);
}
.badge {
  min-width: 16px; height: 16px; padding: 0 4px; box-sizing: border-box; border-radius: 8px;
  font-size: 11px; line-height: 16px; text-align: center; color: #fff; background: var(--el-color-primary);
}
.shown { font-size: 12px; color: var(--autotune-muted); white-space: nowrap; }
.picker { display: flex; min-height: 180px; max-height: 340px; margin: -12px; }
.fields {
  list-style: none; margin: 0; padding: 6px; width: 150px; flex-shrink: 0;
  border-right: 1px solid var(--autotune-border); background: var(--autotune-bg);
}
.fields li {
  display: flex; align-items: center; justify-content: space-between; padding: 6px 10px;
  font-size: 13px; border-radius: 4px; cursor: pointer; color: var(--autotune-text);
}
.fields li:hover { background: var(--autotune-border); }
.fields li.on {
  background: var(--autotune-surface); color: var(--el-color-primary); font-weight: 500;
  box-shadow: 0 0 0 1px var(--autotune-border);
}
.values { flex: 1; display: flex; flex-direction: column; min-width: 0; padding: 6px; }
.find { margin: 2px 2px 6px; width: auto; }
.opts { overflow-y: auto; }
.opt {
  display: flex; align-items: center; gap: 8px; padding: 0 8px; height: 30px;
  border-radius: 4px; font-size: 13px; cursor: pointer;
}
.opt:hover { background: var(--autotune-bg); }
.opt-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.opt-count { color: var(--autotune-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.none { padding: 12px 8px; font-size: 12px; color: var(--autotune-muted); }
</style>
