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
export { all, useFirstLoad } from '../utils/load'
export { navigate, useLinkTo, useRowLink, wantsNewTab } from '../utils/nav'
export { apiErrorText } from '../utils/apiError'
export { copyText } from '../utils/clipboard'
export { sweptKeysOf } from '../utils/space'
export {
  campaignStatus, failureLabel, isLive, runLabel, runStatus, type StatusLook,
} from '../utils/status'
export { absoluteTime, clockTime, duration, exactTime, relativeTime } from '../utils/time'
export { countOptions, useQueryFilters } from '../utils/queryFilters'
export { beatsBaselineRatio, baselineDeltaPct, fmtBaselineDelta } from '../utils/baseline'
export { defaultWorkload, specOf, type Workload } from '../utils/workload'
export { fromYaml, toYaml, YamlError } from '../utils/yaml'
export { renderReport, type ReportInput } from '../report/render'

// The building blocks of the platform's own pages, so a plugin's pages look
// and behave like them: at-a-glance tiles, list filters, the workload picker,
// score bars, the nightly window editor.
export { default as BackLink } from '../components/BackLink.vue'
export { default as CampaignRuns } from '../components/CampaignRuns.vue'
export { default as CatalogExportDialog } from '../components/CatalogExportDialog.vue'
export type { CatalogCandidate } from '../components/CatalogExportDialog.vue'
export { default as ConfigChips } from '../components/ConfigChips.vue'
export { default as CopyButton } from '../components/CopyButton.vue'
export { default as FactTile } from '../components/FactTile.vue'
export { default as FlagChips } from '../components/FlagChips.vue'
export { default as InfoHint } from '../components/InfoHint.vue'
export { default as LinkButton } from '../components/LinkButton.vue'
export { default as ListFilters } from '../components/ListFilters.vue'
export type { FilterField, FilterOption } from '../components/ListFilters.vue'
export { default as LogDialog } from '../components/LogDialog.vue'
export { default as NightlyWindow } from '../components/NightlyWindow.vue'
export { default as PageLoading } from '../components/PageLoading.vue'
export { default as PluginSlot } from '../components/PluginSlot.vue'
export { default as ScoreBars } from '../components/ScoreBars.vue'
export { default as WorkloadPicker } from '../components/WorkloadPicker.vue'

export type { Extensions, FrontendPlugin, NavItem } from './index'
