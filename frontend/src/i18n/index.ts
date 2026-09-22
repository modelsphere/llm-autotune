/** Display-language switching, and nothing else.
 *
 *  Deliberately not vue-i18n: what this needs is a dotted lookup and `{name}`
 *  interpolation over two dictionaries, and a dependency would bring plural
 *  rules, message compilation and a locale-file loader for none of it. Chinese
 *  has no plural forms, and the English plurals in this UI are already written
 *  inline where they occur.
 *
 *  Display only. Nothing here reaches the API: metric keys, statuses, engine
 *  arguments and benchmark slugs are identifiers that must read the same in
 *  every language, and a translated one is a bug report nobody can search for.
 *
 *  A missing key falls back to English rather than rendering blank, and then to
 *  the key itself — so a half-finished translation degrades to a working page
 *  in the wrong language, never to an empty button.
 */
import { computed, ref } from 'vue'

import en from './en'
import zh from './zh'

export type Locale = 'en' | 'zh'

const STORAGE_KEY = 'autotune_locale'
const DICTS: Record<Locale, Record<string, unknown>> = { en, zh }

function initial(): Locale {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'en' || saved === 'zh') return saved
  // The browser's own preference, so a Chinese-locale machine opens in Chinese
  // without anyone having to find the switch first.
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh' : 'en'
}

const current = ref<Locale>(initial())

export const locale = computed(() => current.value)

export function setLocale(next: Locale): void {
  current.value = next
  localStorage.setItem(STORAGE_KEY, next)
  document.documentElement.lang = next === 'zh' ? 'zh-CN' : 'en'
}

function lookup(dict: Record<string, unknown>, key: string): string | undefined {
  let node: unknown = dict
  for (const part of key.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return typeof node === 'string' ? node : undefined
}

/** `t('campaigns.running', { n: 3 })` — `{n}` is replaced, anything unmatched
 *  is left alone so a stray brace in a message is visible rather than eaten. */
export function translate(key: string, vars?: Record<string, unknown>): string {
  const raw = lookup(DICTS[current.value], key) ?? lookup(DICTS.en, key) ?? key
  if (!vars) return raw
  return raw.replace(/\{(\w+)\}/g, (whole, name) =>
    name in vars ? String(vars[name]) : whole,
  )
}

/** The composable every component uses. `t` is reactive through `current`, so
 *  switching language re-renders without a reload. */
export function useI18n() {
  const t = (key: string, vars?: Record<string, unknown>) => {
    void current.value // read it, so the computed/render tracks the locale
    return translate(key, vars)
  }
  return { t, locale, setLocale }
}

setLocale(current.value)
