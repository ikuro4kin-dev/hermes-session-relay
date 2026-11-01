/**
 * Tests for the desktop half's "which chat did the relay just open" matcher.
 *
 * The matcher is a pure function inside desktop/plugin.js. That file imports the
 * desktop SDK at the top, so it cannot be imported under plain Node — the helpers
 * are lifted out of its source text and evaluated here instead, which keeps the
 * test tied to what actually ships.
 *
 * Run: node tests/desktop_find_fresh_session.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, '..', 'desktop', 'plugin.js'), 'utf8')

function lift(name) {
  const start = source.indexOf('function ' + name + '(')
  assert.ok(start >= 0, `function ${name} not found in desktop/plugin.js`)
  let depth = 0
  let i = source.indexOf('{', start)
  const from = i
  for (; i < source.length; i += 1) {
    const ch = source[i]
    if (ch === '{') depth += 1
    else if (ch === '}') {
      depth -= 1
      if (depth === 0) {
        i += 1
        break
      }
    }
  }
  return source.slice(start, i).replace(/^function /, '')
}

const TITLE_LIMIT = 40
const noteMarker = new Function('return function noteMarker' + lift('noteMarker'))()
const findFreshSession = new Function('return function findFreshSession' + lift('findFreshSession'))()

const NOTE = 'ПЕРЕДАЧА СЕССИИ — «Дело мастера», 31.10.2026, вечер (после 19:00)\n\nПроект O:\\delo-mastera'
const MARKER = noteMarker(NOTE)
const OLD = 'local-old'
const T0 = 1793546700

let passed = 0
function check(name, fn) {
  fn()
  passed += 1
  console.log('ok  ' + name)
}

check('marker is the first 40 chars, whitespace collapsed', () => {
  assert.equal(MARKER.length, 40)
  assert.ok(!MARKER.includes('\n'))
  assert.ok(MARKER.startsWith('ПЕРЕДАЧА СЕССИИ'))
})

check('finds the new desktop chat even when its source is "local" (the shipped bug)', () => {
  const rows = [
    { id: 'new', source: 'local', started_at: T0, preview: NOTE },
    { id: OLD, source: 'local', started_at: T0 - 4000, preview: 'старый чат' }
  ]
  assert.equal(findFreshSession(rows, OLD, T0, MARKER).id, 'new')
})

check('finds it by the note preview even when the row has no usable source', () => {
  const rows = [{ id: 'new', source: 'unknown', started_at: T0 + 3, preview: NOTE.replace(/\s+/g, ' ') }]
  assert.equal(findFreshSession(rows, OLD, T0, MARKER).id, 'new')
})

check('falls back to a desktop-labelled row when the preview is empty', () => {
  const rows = [
    { id: 'new', source: 'desktop', started_at: T0 + 2, preview: '' },
    { id: OLD, source: 'local', started_at: T0 - 60, preview: '' }
  ]
  assert.equal(findFreshSession(rows, OLD, T0, MARKER).id, 'new')
})

check('never returns the old chat', () => {
  const rows = [{ id: OLD, source: 'desktop', started_at: T0, preview: NOTE }]
  assert.equal(findFreshSession(rows, OLD, T0, MARKER), null)
})

check('ignores sessions that started long before the relay', () => {
  const rows = [{ id: 'stale', source: 'desktop', started_at: T0 - 100000, preview: '' }]
  assert.equal(findFreshSession(rows, OLD, T0, MARKER), null)
})

check('ignores cron/subagent rows with no note preview', () => {
  const rows = [{ id: 'cron', source: 'cron', started_at: T0 + 1, preview: 'VPS утренний отчёт' }]
  assert.equal(findFreshSession(rows, OLD, T0, MARKER), null)
})

check('prefers the note match over a newer unrelated desktop chat', () => {
  const rows = [
    { id: 'other', source: 'desktop', started_at: T0 + 9, preview: 'Обсуждение будущей встречи' },
    { id: 'real', source: 'local', started_at: T0 + 2, preview: NOTE }
  ]
  assert.equal(findFreshSession(rows, OLD, T0, MARKER).id, 'real')
})

check('empty list is handled', () => {
  assert.equal(findFreshSession([], OLD, T0, MARKER), null)
  assert.equal(findFreshSession(null, OLD, T0, MARKER), null)
})

console.log(`\n${passed} checks, 0 failures`)
