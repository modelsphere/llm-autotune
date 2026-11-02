/// <reference types="vite/client" />
/** Frontend plugins: pages, nav entries, strings and page sections from a
 *  folder compiled into this app.
 *
 *  A plugin is a folder `src/plugins/installed/<name>/` whose `index.ts`
 *  default-exports a `FrontendPlugin`. It is found at build time; there is no
 *  runtime loading. A backend plugin of the same name usually serves the API
 *  its pages call (docs/plugins.md).
 *
 *  What a plugin may import from the app is `src/plugins/api.ts`; anything
 *  else can change in any release.
 */
import type { Component } from 'vue'
import type { RouteRecordRaw } from 'vue-router'

import { registerMessages, type Locale } from '../i18n'

export type Extensions = Record<string, Record<string, unknown>>

export interface NavItem {
  path: string
  /** An i18n key (the plugin's `messages` define it). */
  label: string
  /** On the top row, or in the Library menu (what campaigns are built from).
   *  'tuning' is the Library's old name and still lands there. */
  group: 'top' | 'library' | 'tuning'
  /** An i18n key: one line under the label in the Library menu. */
  hint?: string
  /** Position among the entries of its group; the app's own are 10, 20, 30… */
  order: number
}

export interface FrontendPlugin {
  name: string
  routes?: RouteRecordRaw[]
  nav?: NavItem[]
  /** Strings by locale, merged into the app's dictionaries. */
  messages?: Partial<Record<Locale, Record<string, unknown>>>
  /** Components rendered where the app places `<PluginSlot name="…">`. */
  slots?: Record<string, Component>
}

const found = import.meta.glob<{ default: FrontendPlugin }>('./installed/*/index.ts', {
  eager: true,
})

export const plugins: FrontendPlugin[] = Object.values(found)
  .map((module) => module.default)
  .sort((a, b) => a.name.localeCompare(b.name))

for (const plugin of plugins) {
  for (const [locale, dict] of Object.entries(plugin.messages ?? {})) {
    registerMessages(locale as Locale, dict as Record<string, unknown>)
  }
}

export function pluginRoutes(): RouteRecordRaw[] {
  return plugins.flatMap((p) => p.routes ?? [])
}

export function pluginNav(group: NavItem['group']): NavItem[] {
  const of = (n: NavItem) => (n.group === 'tuning' ? 'library' : n.group)
  return plugins.flatMap((p) => (p.nav ?? []).filter((n) => of(n) === group))
}

export function slotComponents(name: string): Component[] {
  return plugins.flatMap((p) => (p.slots?.[name] ? [p.slots[name]] : []))
}
