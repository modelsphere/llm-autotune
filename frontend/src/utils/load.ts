/** A page's first load, as state the page can show.
 *
 *  Pages used to fetch in onMounted and render whatever they had meanwhile:
 *  a list read "No data" until its rows arrived, and a detail page stayed
 *  blank. On a slow link that looks like an empty or broken page. This keeps
 *  `loading` true until the first load has finished and keeps its error, so a
 *  page can show a spinner, then the rows, or what went wrong with a retry.
 *
 *  Only the first load: later refreshes (a poll, a save) keep what is on
 *  screen rather than flashing a spinner over it.
 *
 *      const first = useFirstLoad(load)
 *      onMounted(first.run)
 *      <el-table v-loading="first.loading" :empty-text="first.emptyText('No keys yet')">
 *      <PageLoading v-if="!item" :error="first.error" @retry="first.run" />
 */
import { reactive } from 'vue'
import { translate } from '../i18n'
import { apiErrorText } from './apiError'

export interface FirstLoad {
  loading: boolean
  error: string
  run: () => Promise<void>
  /** A table's empty text: "Loading…" before the first load, what went wrong
   *  if it failed, and the table's own text (or Element's) once it is empty. */
  emptyText: (empty?: string) => string | undefined
}

export function useFirstLoad(load: () => Promise<unknown>): FirstLoad {
  const state: FirstLoad = reactive({
    loading: true,
    error: '',
    async run() {
      try {
        await load()
        state.error = ''
      } catch (e) {
        state.error = apiErrorText(e, (e as Error)?.message || 'Request failed')
      } finally {
        state.loading = false
      }
    },
    emptyText(empty?: string) {
      if (state.loading) return translate('common.loading')
      if (state.error) return `${translate('common.loadFailed')}: ${state.error}`
      return empty
    },
  })
  return state
}

/** Several requests a page needs, at once rather than one after another: on a
 *  link with a long round trip, each `await` in a row adds a full one. */
export function all<T extends readonly unknown[]>(
  ...requests: { [K in keyof T]: Promise<T[K]> }
): Promise<T> {
  return Promise.all(requests) as Promise<T>
}
