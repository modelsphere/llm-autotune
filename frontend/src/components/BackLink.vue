<script setup lang="ts">
/** "← Back", top-left, above the title.
 *
 *  Every detail page used to end its action row with a Back
 *  button, which put "leave this page" next to "abort this run" and pushed it
 *  as far from the reading start as a row can get. Back belongs where the eye
 *  enters the page, and it is navigation, not an action — so it renders as a
 *  link, not a button, and the arrow is inline SVG for the same reason
 *  CopyButton's is: no icon package for one 14px shape. A real link, too:
 *  middle-click / Ctrl+click open the parent page in a new tab.
 */
const props = withDefaults(defineProps<{ to: string; label?: string }>(), {
  label: 'Back',
})
</script>

<template>
  <router-link class="back" :to="props.to">
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M10 2.5 4.5 8l5.5 5.5" fill="none" stroke="currentColor" stroke-width="1.6"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
    {{ props.label }}
  </router-link>
</template>

<style scoped>
.back {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-bottom: 8px;
  color: var(--autotune-muted, #909399);
  font-size: 13px;
  cursor: pointer;
  text-decoration: none;
}
.back:hover,
.back:focus-visible {
  color: var(--el-color-primary);
}
</style>
