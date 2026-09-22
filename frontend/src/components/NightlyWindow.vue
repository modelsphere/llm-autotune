<script setup lang="ts">
/** The nightly window: two clock times, a timezone, and an optional last day.
 *
 *  Shown as times rather than as datetimes because that is what the
 *  arrangement actually is — "23:00 to 08:00, every night" — and because a
 *  date picker would make tomorrow's occurrence look like the only one.
 */
import { computed } from 'vue'
import InfoHint from './InfoHint.vue'

const props = defineProps<{
  start: string
  end: string
  timezone: string
  until: string | null
}>()

const emit = defineEmits<{
  (e: 'update:start', v: string): void
  (e: 'update:end', v: string): void
  (e: 'update:timezone', v: string): void
  (e: 'update:until', v: string | null): void
}>()

/** The browser's own zone first: it is right nearly always, and being wrong
 *  here means a campaign wakes at the wrong hour with no visible symptom. */
const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone

const zones = computed<string[]>(() => {
  const all =
    typeof (Intl as any).supportedValuesOf === 'function'
      ? ((Intl as any).supportedValuesOf('timeZone') as string[])
      : []
  const common = [localZone, 'Asia/Shanghai', 'UTC']
  return [...new Set([...common, ...all])]
})

function minutes(hhmm: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm ?? '')
  if (!m) return null
  const h = Number(m[1])
  const min = Number(m[2])
  if (h > 23 || min > 59) return null
  return h * 60 + min
}

/** What the pair means, said back. A window that wraps past midnight is the
 *  normal case here and the one most easily mistyped. */
const summary = computed(() => {
  const a = minutes(props.start)
  const b = minutes(props.end)
  if (a === null || b === null) return { text: '', overnight: false, bad: !!(props.start || props.end) }
  if (a === b) return { text: 'start and end are the same — a window needs a length', overnight: false, bad: true }
  const overnight = b <= a
  const span = overnight ? 24 * 60 - a + b : b - a
  const hours = Math.floor(span / 60)
  const mins = span % 60
  return {
    text: `${hours}h${mins ? ` ${mins}m` : ''} every night${overnight ? ', ending the next morning' : ''}`,
    overnight,
    bad: false,
  }
})
</script>

<template>
  <div class="window">
    <div class="row">
      <!-- A picker with real hour and minute columns, not a list of half hours.
           `el-time-select` could only offer a fixed step, so a window that had
           to start at 23:10 — to clear a nightly job, say — was not expressible
           at all. `value-format` keeps the model a plain "HH:mm" string, which
           is what the campaign stores and what the summary below parses. -->
      <div class="field">
        <label>Start</label>
        <el-time-picker :model-value="start" format="HH:mm" value-format="HH:mm"
          placeholder="23:00" style="width: 128px"
          @update:model-value="(v: string | null) => emit('update:start', v ?? '')" />
      </div>
      <span class="arrow">→</span>
      <div class="field">
        <label>End</label>
        <el-time-picker :model-value="end" format="HH:mm" value-format="HH:mm"
          placeholder="08:00" style="width: 128px"
          @update:model-value="(v: string | null) => emit('update:end', v ?? '')" />
      </div>
      <div class="field grow">
        <label>
          Timezone
          <InfoHint>
            The window is stored as local clock times, so 23:00 stays 23:00 across a
            daylight-saving change rather than drifting by an hour.
          </InfoHint>
        </label>
        <el-select :model-value="timezone || localZone" filterable style="width: 100%"
          @update:model-value="(v: string) => emit('update:timezone', v)">
          <el-option v-for="z in zones" :key="z" :value="z" :label="z" />
        </el-select>
      </div>
    </div>

    <div class="row">
      <div class="field grow">
        <label>
          Repeat until
          <InfoHint>
            Optional. After this, no new night starts and the campaign finishes. A night
            already running is never cut short by it. Leave empty to keep going until the
            search is exhausted.
          </InfoHint>
        </label>
        <el-date-picker :model-value="until" type="date" placeholder="until the search runs out"
          style="width: 100%" value-format="YYYY-MM-DDTHH:mm:ss[Z]"
          @update:model-value="(v: string | null) => emit('update:until', v)" />
      </div>
    </div>

    <p v-if="summary.text" class="summary" :class="{ bad: summary.bad }">
      {{ summary.text }}
    </p>
    <p v-else-if="!start && !end" class="summary muted">
      No window — the campaign runs only while you start it by hand.
    </p>
  </div>
</template>

<style scoped>
.row {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  margin-bottom: 10px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.grow {
  flex: 1;
  min-width: 0;
}
label {
  font-size: 12px;
  color: var(--autotune-muted);
}
.arrow {
  color: var(--autotune-muted);
  padding-bottom: 8px;
}
.summary {
  margin: 2px 0 0;
  font-size: 12.5px;
  color: var(--el-color-primary-dark-2);
}
.summary.bad {
  color: var(--el-color-danger);
}
.summary.muted {
  color: var(--autotune-muted);
}
</style>
