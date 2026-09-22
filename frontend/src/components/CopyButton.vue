<script setup lang="ts">
/** Copy, as an icon.
 *
 *  A word-wide "Copy" button in every row of a settings table costs a column
 *  and repeats a label eleven times to say what one glyph says once. The SVG is
 *  inline because the project has no icon package, and pulling one in for a
 *  single 16px shape would be a dependency per icon thereafter.
 *
 *  It keeps a real <button>: an accessible name via the tooltip content, keyboard
 *  focus, and the click stopped from reaching a clickable table row underneath.
 */
import { copyText } from '../utils/clipboard'

const props = defineProps<{
  value: string
  /** Toast text, e.g. "Engine port copied". */
  label?: string
  /** Tooltip; defaults to the toast text. */
  title?: string
}>()

function copy() {
  copyText(props.value, props.label ?? 'Copied')
}
</script>

<template>
  <el-tooltip :content="title ?? label ?? 'Copy'" placement="top" :show-after="400">
    <button class="copy-btn" type="button" :aria-label="title ?? label ?? 'Copy'"
      @click.stop="copy">
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <rect x="5.25" y="5.25" width="8.25" height="8.25" rx="1.6"
          fill="none" stroke="currentColor" stroke-width="1.3" />
        <path d="M10.75 5.25V3.9A1.4 1.4 0 0 0 9.35 2.5H3.9A1.4 1.4 0 0 0 2.5 3.9v5.45
          A1.4 1.4 0 0 0 3.9 10.75h1.35" fill="none" stroke="currentColor"
          stroke-width="1.3" stroke-linecap="round" />
      </svg>
    </button>
  </el-tooltip>
</template>

<style scoped>
.copy-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--autotune-muted, #909399);
  cursor: pointer;
  transition: color 0.15s, background 0.15s;
}
.copy-btn:hover,
.copy-btn:focus-visible {
  color: var(--el-color-primary);
  background: var(--autotune-bg, #f5f7fa);
}
</style>
