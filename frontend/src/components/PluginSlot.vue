<script setup lang="ts">
/** Where plugins may add a section to a page: every installed plugin that
 *  fills `name` renders here, in plugin-name order, with `props` bound. With
 *  no plugin installed it renders nothing. */
import { computed } from 'vue'

import { slotComponents } from '../plugins'

const props = defineProps<{ name: string; props?: Record<string, unknown> }>()
const components = computed(() => slotComponents(props.name))
</script>

<template>
  <component :is="c" v-for="(c, i) in components" :key="i" v-bind="props.props ?? {}" />
</template>
