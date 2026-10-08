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
  /** Under the Tuning menu, or on the top row. */
  group: 'tuning' | 'top'
  /** Position among the entries of its group; the app's own are 10, 20, 30… */
  order: number
}

export interface StrategyOption {
  value: string
  label: string
  help?: string
}

/** Search strategies a plugin plans in-process (the backend's
 *  `propose_candidates`), offered in the New campaign Strategy select next to
 *  "no policy" and the policy containers. Choosing one writes the campaign's
 *  `extensions`, which is how the backend plugin learns it plans it. */
export interface StrategyContribution {
  /** The option group's title, an i18n key or plain text. */
  group: string
  options: StrategyOption[]
  /** The campaign's `extensions` with this plugin's strategy set to `value`,
   *  or cleared (null) when another strategy is chosen. Leave the rest of
   *  `extensions` as it is. */
  apply(extensions: Extensions, value: string | null): Extensions
  /** Which of this plugin's options a campaign's `extensions` select, if any. */
  selected(extensions: Extensions): string | null
  /** How a campaign planned this way reads in lists and summaries. */
  describe(extensions: Extensions): string | null
}

export interface FrontendPlugin {
  name: string
  routes?: RouteRecordRaw[]
  nav?: NavItem[]
  /** Strings by locale, merged into the app's dictionaries. */
  messages?: Partial<Record<Locale, Record<string, unknown>>>
  /** Components rendered where the app places `<PluginSlot name="…">`. */
  slots?: Record<string, Component>
  strategies?: StrategyContribution
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
  return plugins.flatMap((p) => (p.nav ?? []).filter((n) => n.group === group))
}

export function slotComponents(name: string): Component[] {
  return plugins.flatMap((p) => (p.slots?.[name] ? [p.slots[name]] : []))
}

/** Strategy select values are namespaced, so two plugins cannot collide with
 *  each other or with the app's own (`''`, `policy:<id>`). */
export function strategyValue(plugin: string, value: string): string {
  return `plugin:${plugin}:${value}`
}

export function strategyOf(value: string): { plugin: FrontendPlugin; value: string } | null {
  const m = /^plugin:([^:]+):(.*)$/.exec(value)
  const plugin = m ? plugins.find((p) => p.name === m[1] && p.strategies) : undefined
  return m && plugin ? { plugin, value: m[2] } : null
}

/** The Strategy select value a campaign's `extensions` stand for, or ''. */
export function strategyFromExtensions(extensions: Extensions | undefined): string {
  for (const plugin of plugins) {
    const chosen = plugin.strategies?.selected(extensions ?? {})
    if (chosen) return strategyValue(plugin.name, chosen)
  }
  return ''
}

/** `extensions` for the Strategy select's `value`: the chosen plugin's
 *  strategy set, every other plugin's cleared. */
export function applyStrategy(extensions: Extensions, value: string): Extensions {
  const chosen = strategyOf(value)
  let out: Extensions = { ...extensions }
  for (const plugin of plugins) {
    if (!plugin.strategies) continue
    out = plugin.strategies.apply(out, chosen?.plugin === plugin ? chosen.value : null)
  }
  return out
}

/** How a campaign's plugin strategy reads, or null when none plans it. */
export function describeStrategy(extensions: Extensions | undefined): string | null {
  for (const plugin of plugins) {
    const text = plugin.strategies?.describe(extensions ?? {})
    if (text) return text
  }
  return null
}
