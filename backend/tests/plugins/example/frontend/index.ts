/** The example plugin's frontend: one of each contribution.
 *
 *  Copied to frontend/src/plugins/installed/example/ before `npm run build`
 *  (CI does this on every change), it adds a page with a nav entry, a strategy
 *  in the New campaign Strategy select, and a card on the account page. The
 *  backend half is the `example_plugin` package next to this folder.
 */
import type { FrontendPlugin } from '@/plugins/api'

import AccountCard from './AccountCard.vue'

const LABEL = 'plan:reverse'

const plugin: FrontendPlugin = {
  name: 'example',
  routes: [{ path: '/example', component: () => import('./TicksView.vue') }],
  nav: [{ path: '/example', label: 'example.nav', group: 'top', order: 45 }],
  messages: {
    en: { example: { nav: 'Example', ticks: 'Worker ticks noted', strategy: 'Example plugin' } },
    zh: { example: { nav: '示例', ticks: '已记录的 worker 周期', strategy: '示例插件' } },
  },
  slots: { 'account.cards': AccountCard },
  strategies: {
    group: 'Example plugin',
    options: [{ value: 'reverse', label: 'Reverse order', help: 'last-declared first' }],
    apply(extensions, value) {
      const out = { ...extensions }
      if (value === 'reverse') out.example = { ...(out.example ?? {}), label: LABEL }
      else if (out.example?.label === LABEL) delete out.example
      return out
    },
    selected: (extensions) => (extensions.example?.label === LABEL ? 'reverse' : null),
    describe: (extensions) =>
      extensions.example?.label === LABEL ? 'example plugin: reverse order' : null,
  },
}

export default plugin
