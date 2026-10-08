<script setup lang="ts">
/** An `el-button` that goes somewhere in the app — rendered as a real `<a>`,
 *  so a middle-click, a Ctrl/Cmd+click or the browser's "Open link in new
 *  tab" work on it the way they do on any link. A plain click still
 *  navigates in place. Every other attribute (type, size, link, text, plain…)
 *  is handed to the button unchanged, so it looks exactly like one. */
import { RouterLink, type RouteLocationRaw } from 'vue-router'

defineOptions({ inheritAttrs: false })
defineProps<{ to: RouteLocationRaw }>()
</script>

<template>
  <RouterLink v-slot="{ href, navigate }" :to="to" custom>
    <el-button v-bind="$attrs" tag="a" :href="href" class="link-button" @click="navigate">
      <slot />
    </el-button>
  </RouterLink>
</template>

<style scoped>
.link-button {
  text-decoration: none;
}
</style>
