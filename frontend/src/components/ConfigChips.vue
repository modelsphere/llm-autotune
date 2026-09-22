<script setup lang="ts">
/** One configuration, as key/value chips.
 *
 *  The Candidates table read this way and the leaderboard did not — it printed
 *  `attention_backend=flashinfer, chunked_prefill_size=65536, page_size=32` as
 *  one run-on mono string, so the same config looked like two different things
 *  on two tabs of the same page. One component, so they cannot drift again.
 *
 *  Only the swept keys are shown: a campaign's config is mostly a fixed base
 *  (context length, parsers, cache settings) with one or two knobs moving, and
 *  printing all of it hides the thing being compared.
 */
const props = defineProps<{
  config: Record<string, unknown>
  /** The parameters this campaign actually varies. */
  keys: string[]
  /** GPUs the config occupies, when the caller knows. */
  cards?: number | null
}>()

const baseline = () => props.config?.__baseline__ as string | undefined
</script>

<template>
  <span v-if="baseline()" class="wrap">
    <el-tag size="small" effect="plain">production baseline</el-tag>
    <span class="mono muted container">{{ baseline() }}</span>
  </span>

  <span v-else-if="keys.length" class="wrap">
    <span v-for="key in keys" :key="key" class="chip">
      <span class="chip-key mono">{{ key }}</span>
      <span class="chip-val mono">{{ config[key] ?? '—' }}</span>
    </span>
    <span v-if="cards" class="muted cards">{{ cards }} GPU{{ cards === 1 ? '' : 's' }}</span>
  </span>

  <!-- A single-candidate campaign varies nothing; show what there is. -->
  <span v-else class="mono">{{
    Object.entries(config).map(([k, v]) => `${k}=${v}`).join(', ') || '(defaults)'
  }}</span>
</template>

<style scoped>
.wrap {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
}
.chip {
  display: inline-flex;
  align-items: stretch;
  margin: 2px 8px 2px 0;
  border: 1px solid var(--autotune-border, #dcdfe6);
  border-radius: 5px;
  overflow: hidden;
  font-size: 12px;
  line-height: 20px;
}
.chip-key {
  padding: 0 6px;
  background: #f1f5f9;
  color: var(--el-text-color-regular);
}
.chip-val {
  padding: 0 6px;
  font-weight: 600;
}
.cards {
  font-size: 12px;
  margin-left: 4px;
}
.container {
  font-size: 12px;
  margin-left: 6px;
}
</style>
