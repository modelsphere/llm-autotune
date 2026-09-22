<script setup lang="ts">
/** A search space at a glance: one row per axis, the points as chips.
 *
 *  This replaces a paragraph that spelled the same thing out in prose —
 *  "Sweeping attention_backend (flashinfer, triton) × chunked_prefill_size
 *  (8192, 16384, 24576, 32768) × …". The prose grew with the space, wrapped
 *  across three lines, and buried the one thing worth seeing: which knobs
 *  move, and how far.
 */
import { computed } from 'vue'
import { axesOf, type SpaceShape } from '../utils/space'

const props = defineProps<{
  space: SpaceShape | null | undefined
  /** The authoritative count, from whoever expanded the space. Omitted when
   *  nobody has counted yet — a number we made up here would be a second
   *  answer, and conditions make it the wrong one. */
  candidates?: number | null
  /** Optional footnote, e.g. how the runs pack onto a machine. */
  note?: string
  compact?: boolean
}>()

const axes = computed(() => axesOf(props.space))
</script>

<template>
  <div v-if="axes.length" class="map" :class="{ compact }">
    <div v-for="axis in axes" :key="axis.label" class="axis">
      <div class="name mono" :title="axis.label">{{ axis.label }}</div>

      <div class="points">
        <span v-for="(value, i) in axis.values" :key="i" class="pt mono">{{ value }}</span>
        <span v-if="axis.hidden" class="more">+{{ axis.hidden }} more</span>
        <span v-if="!axis.values.length" class="mono broken">{{ axis.interval }}</span>

        <el-tag v-if="axis.kind === 'tied'" size="small" type="info" effect="plain"
          class="mark" title="These move together — zipped, not crossed">
          tied
        </el-tag>
        <el-tag v-if="axis.kind === 'range'" size="small" type="info" effect="plain"
          class="mark" :title="`interval ${axis.interval}`">
          range
        </el-tag>
        <el-tag v-if="axis.gate" size="small" type="warning" effect="plain" class="mark">
          only if {{ axis.gate.on }} = {{ axis.gate.values.join(' / ') }}
        </el-tag>
      </div>

      <div class="count mono">×{{ axis.count }}</div>
    </div>

    <div v-if="candidates != null || note" class="total">
      <b v-if="candidates != null">{{ candidates }} candidate{{ candidates === 1 ? '' : 's' }}</b>
      <span v-if="note" class="note">{{ note }}</span>
    </div>
  </div>

  <p v-else class="muted empty">No sweep — a single configuration.</p>
</template>

<style scoped>
.map {
  border: 1px solid var(--autotune-border);
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}
.axis {
  display: grid;
  grid-template-columns: minmax(120px, 200px) 1fr auto;
  gap: 12px;
  align-items: baseline;
  padding: 7px 12px;
  border-bottom: 1px solid var(--autotune-border);
}
.axis:last-of-type {
  border-bottom: none;
}
.name {
  color: var(--autotune-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.points {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.pt {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary-dark-2);
  border: 1px solid var(--el-color-primary-light-8);
  border-radius: 4px;
  padding: 0 6px;
  line-height: 19px;
}
.more,
.note {
  color: var(--autotune-muted);
  font-size: 11.5px;
}
.broken {
  color: var(--el-color-danger);
}
.mark {
  margin-left: 4px;
}
.count {
  color: var(--autotune-muted);
  white-space: nowrap;
}
.total {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 7px 12px;
  background: var(--autotune-bg);
  border-top: 1px solid var(--autotune-border);
  font-size: 12.5px;
}
.compact .axis {
  padding: 4px 10px;
}
.compact .total {
  padding: 4px 10px;
}
.empty {
  margin: 0;
}
</style>
