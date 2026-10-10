/** Everything a frontend plugin may import from the app, and nothing else.
 *
 *  A plugin that imports only from here keeps working across releases with
 *  the same plugin API version (see the backend's PLUGIN_API_VERSION and
 *  docs/plugins.md); the rest of `src/` is private to the app and can change
 *  in any release.
 */
export { api, downloadText, fetchPolicySessionLog, fetchRunLog } from '../api/client'
export type * from '../api/client'
export { useI18n, type Locale } from '../i18n'
export { useAuthStore } from '../stores/auth'
export { usePoll } from '../utils/poll'
export { navigate, useLinkTo, useRowLink, wantsNewTab } from '../utils/nav'
export { apiErrorText } from '../utils/apiError'
export { copyText } from '../utils/clipboard'
export { sweptKeysOf } from '../utils/space'
export { campaignStatus } from '../utils/status'
export { absoluteTime, exactTime, relativeTime } from '../utils/time'
export { fromYaml, toYaml, YamlError } from '../utils/yaml'
export { renderReport, type ReportInput } from '../report/render'

export { default as BackLink } from '../components/BackLink.vue'
export { default as CampaignRuns } from '../components/CampaignRuns.vue'
export { default as ConfigChips } from '../components/ConfigChips.vue'
export { default as CopyButton } from '../components/CopyButton.vue'
export { default as FlagChips } from '../components/FlagChips.vue'
export { default as InfoHint } from '../components/InfoHint.vue'
export { default as LinkButton } from '../components/LinkButton.vue'
export { default as LogDialog } from '../components/LogDialog.vue'
export { default as PluginSlot } from '../components/PluginSlot.vue'

export type {
  Extensions,
  FrontendPlugin,
  NavItem,
  StrategyContribution,
  StrategyOption,
} from './index'
