<script setup lang="ts">
/** An engine config as chips instead of a JSON wall, with unknown flags badged.
 *
 *  Known flags (the advisory catalog) render plain; a flag the platform has
 *  never heard of gets an amber badge — new or engine-specific, passed to the
 *  launch exactly as written. A badge, never a judgement: an engine gains
 *  flags faster than any catalog tracks them.
 *
 *  Distinct from ConfigChips, which shows a campaign's SWEPT keys — here the
 *  whole declared config is the point, and the split that matters is
 *  familiar-vs-novel rather than varied-vs-fixed.
 */
import { computed } from 'vue'

const props = defineProps<{
  args: Record<string, unknown>
  /** Advisory known-flag names for this engine; empty = badge nothing. */
  known: string[]
}>()

const chips = computed(() => {
  const knownSet = new Set(props.known)
  return Object.entries(props.args)
    .map(([key, value]) => ({
      key,
      text: value === true ? key : `${key} = ${value}`,
      unknown: knownSet.size > 0 && !knownSet.has(key),
    }))
    .sort((a, b) => Number(a.unknown) - Number(b.unknown) || a.key.localeCompare(b.key))
})
</script>

<template>
  <div class="chips">
    <template v-for="chip in chips" :key="chip.key">
      <el-tooltip v-if="chip.unknown" placement="top"
        content="Not in the platform's flag catalog — new or engine-specific; passed to the launch as-is">
        <el-tag type="warning" effect="plain" size="small" class="chip mono">
          {{ chip.text }} <span class="badge">?</span>
        </el-tag>
      </el-tooltip>
      <el-tag v-else effect="plain" size="small" class="chip mono">{{ chip.text }}</el-tag>
    </template>
    <span v-if="!chips.length" class="muted">(engine defaults)</span>
  </div>
</template>

<style scoped>
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  max-width: 100%;
  font-size: 11.5px;
}
.badge {
  font-weight: 700;
  margin-left: 2px;
}
</style>
