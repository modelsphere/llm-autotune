/** Highlighting for serving commands (`sglang serve …`, `vllm serve …`) and
 *  other shell fences. highlight.js's bash grammar colours keywords and
 *  strings only, so a command made of flags and values came out plain; this
 *  one knows the shape of a command line: env assignments, the program and its
 *  subcommand, flags, their values, and the `\` continuations. Emits hljs
 *  class names so one stylesheet colours every fence. */

const esc = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

const span = (cls: string, text: string) => `<span class="hljs-${cls}">${esc(text)}</span>`

// A word, a quoted string, a comment, or whitespace.
const TOKEN = /(\s+)|(#.*$)|('(?:[^'\\]|\\.)*'?|"(?:[^"\\]|\\.)*"?)|(\\$)|([^\s'"]+)/gm

function valueClass(word: string): string {
  if (/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(word)) return 'number'
  if (/^(true|false|none|null|auto)$/i.test(word)) return 'literal'
  return 'string'
}

export function highlightShell(source: string): string {
  let out = ''
  // Per logical command: 'start' until the program is seen, then 'args'.
  let state: 'start' | 'program' | 'args' = 'start'
  let afterFlag = false
  let continued = false // the line so far ended in `\`
  for (const m of source.matchAll(TOKEN)) {
    const [text, space, comment, quoted, cont, word] = m
    if (space !== undefined) {
      if (space.includes('\n') && !continued) {
        state = 'start'
        afterFlag = false
      }
      out += esc(space)
    } else if (comment !== undefined) {
      out += span('comment', comment)
    } else if (cont !== undefined) {
      out += span('meta', cont)
      continued = true
      continue
    } else if (quoted !== undefined) {
      out += span('string', quoted)
      afterFlag = false
    } else if (word !== undefined) {
      if (state === 'start' && /^[A-Za-z_][A-Za-z0-9_]*=/.test(word)) {
        const eq = word.indexOf('=')
        out += span('variable', word.slice(0, eq)) + esc('=') + span(valueClass(word.slice(eq + 1)), word.slice(eq + 1))
      } else if (state === 'start') {
        out += span('built_in', word)
        state = 'program'
      } else if (word.startsWith('-')) {
        const eq = word.indexOf('=')
        if (eq > 0) {
          out += span('attr', word.slice(0, eq)) + esc('=') + span(valueClass(word.slice(eq + 1)), word.slice(eq + 1))
          afterFlag = false
        } else {
          out += span('attr', word)
          afterFlag = true
        }
        state = 'args'
      } else if (state === 'program' && /^[a-z][\w-]*$/.test(word)) {
        out += span('keyword', word) // the subcommand: `serve`, `-m module` stays a value
        state = 'args'
      } else if (/^[|&;]+$/.test(word)) {
        out += span('keyword', word)
        state = 'start'
        afterFlag = false
      } else {
        out += span(afterFlag || state === 'args' ? valueClass(word) : 'string', word)
        afterFlag = false
        state = 'args'
      }
    } else {
      out += esc(text)
    }
    if (space === undefined) continued = false
  }
  return out
}
