<script setup lang="ts">
/** Campaigns as a wrapping row of chips, each a link with its status as a
 *  coloured dot and its full name on hover. Campaign names are long, so a
 *  column of them wrapped into a few cramped lines; chips use the width there
 *  is. The first few show, the rest open in place. */
import { Lock } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'
import { useI18n } from '../i18n'

export interface ChipCampaign {
  id: number
  name: string
  status: string
}

const props = withDefaults(defineProps<{
  campaigns: ChipCampaign[]
  /** Campaigns measuring on the current sample: marked, and listed first. */
  holding?: number[]
  /** How many show before "+N more". */
  max?: number
}>(), { holding: () => [], max: 4 })

const ORDER: Record<string, number> = { active: 0, paused: 1, scheduled: 2, draft: 3, done: 4 }
const sorted = computed(() => [...props.campaigns].sort((a, b) =>
  Number(props.holding.includes(b.id)) - Number(props.holding.includes(a.id))
  || (ORDER[a.status] ?? 5) - (ORDER[b.status] ?? 5)
  || b.id - a.id))
const { t } = useI18n()
const all = ref(false)
const shown = computed(() => (all.value ? sorted.value : sorted.value.slice(0, props.max)))
const hidden = computed(() => sorted.value.length - shown.value.length)
</script>

<template>
  <div class="chips">
    <router-link v-for="c in shown" :key="c.id" :to="`/campaigns/${c.id}`" class="chip"
      :class="{ holding: holding.includes(c.id) }"
      :title="`${c.name} — ${c.status}${holding.includes(c.id) ? t('datasets.chips.measuring') : ''}`">
      <el-icon v-if="holding.includes(c.id)" class="lock"><Lock /></el-icon>
      <span v-else class="dot" :class="`s-${c.status}`" />
      <span class="name">{{ c.name }}</span>
    </router-link>
    <button v-if="hidden > 0" type="button" class="more" @click="all = true">{{ t('datasets.chips.more', { n: hidden }) }}</button>
    <button v-else-if="all && sorted.length > max" type="button" class="more" @click="all = false">{{ t('datasets.chips.fewer') }}</button>
  </div>
</template>

<style scoped>
.chips { display: flex; flex-wrap: wrap; gap: 6px; min-width: 0; }
.chip {
  display: inline-flex; align-items: center; gap: 6px; max-width: min(340px, 100%); box-sizing: border-box; height: 26px;
  padding: 0 10px; border-radius: 13px; font-size: 12.5px; text-decoration: none;
  color: var(--autotune-text); background: var(--autotune-bg); border: 1px solid var(--autotune-border);
  transition: border-color 0.15s, background 0.15s;
}
.chip:hover { border-color: var(--el-color-primary-light-5); background: var(--el-color-primary-light-9); }
.chip.holding { border-color: var(--el-color-warning-light-5); background: var(--el-color-warning-light-9); }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--el-text-color-placeholder); }
.dot.s-active { background: var(--el-color-success); }
.dot.s-paused { background: var(--el-color-warning); }
.dot.s-scheduled { background: var(--el-color-primary-light-3); }
.dot.s-draft { background: transparent; border: 1.5px solid var(--el-text-color-placeholder); box-sizing: border-box; }
.lock { flex: none; color: var(--el-color-warning-dark-2); font-size: 12px; }
.more {
  height: 26px; padding: 0 10px; border-radius: 13px; font: inherit; font-size: 12.5px; cursor: pointer;
  color: var(--el-color-primary); background: transparent; border: 1px dashed var(--el-color-primary-light-5);
}
.more:hover { background: var(--el-color-primary-light-9); }
</style>
