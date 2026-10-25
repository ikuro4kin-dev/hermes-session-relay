/**
 * Session Relay — a Hermes Desktop plugin.
 *
 * What it does (automatic hand-off to a new session when the context fills up):
 *  1. Watches how full the focused session's context window is.
 *  2. When usage crosses the threshold and the turn has finished, it submits a
 *     hidden service message: the agent replies with the hand-off note itself.
 *  3. The plugin opens the new chat through the SDK — host.newChat() — and
 *     submits the note into it (host.composer.submit('new', note)), so the
 *     fresh session starts with the hand-off as its first user message. The new
 *     chat is named after the current one with the number incremented
 *     ("Bugfix-6" → "Bugfix-7").
 *  4. A toast and an OS notification announce the new chat; clicking the
 *     notification opens it.
 *
 * No agent tool is involved: the plugin reads the agent's reply from the
 * message.complete event and types the note into the new chat itself.
 *
 * The ⚡ chip in the status bar starts a relay manually (works below the
 * threshold too). Settings live under Settings → Plugins → Session Relay.
 *
 * The default threshold is 45% — below the point (~50%) where Hermes compacts
 * the history itself, so the relay fires before compaction. Change it in the
 * plugin settings or in DEFAULTS below.
 */

import { atom, Button, cn, haptic, host, ListRow, SegmentedControl, Tip, ToggleRow, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect } from 'react'

const ID = 'session-relay'
const TITLE_LIMIT = 40
const DEFAULTS = { auto: true, threshold: 45 }
const NOTE_TIMEOUT_MS = 300000
const SEED_TRIES = 40
const SEED_RETRY_MS = 500
// The streamed usage the desktop keeps for the primary chat carries no
// context_percent (its own gauge asks the gateway for a breakdown instead),
// so the chip polls the same read-only RPC.
const PCT_REFRESH_MS = 30000

/** "Bugfix-6" → "Bugfix-7"; a title without a number gets "-2". */
function nextTitle(raw) {
  const t = String(raw || '').trim()
  if (!t) {
    return ''
  }
  const m = t.match(/^(.*?)(\d+)(\D*)$/)
  let out
  if (!m) {
    out = t + '-2'
  } else {
    const n = String(parseInt(m[2], 10) + 1)
    const num = m[2].length > 1 && m[2].startsWith('0') ? n.padStart(m[2].length, '0') : n
    out = m[1] + num + m[3]
  }
  return out.length > TITLE_LIMIT ? out.slice(0, TITLE_LIMIT) : out
}

export default {
  id: ID,
  name: 'Session Relay',

  register(ctx) {
    const storedPrefs = { ...ctx.storage.get('prefs', {}) }
    const prefs = atom({ ...DEFAULTS, ...storedPrefs })
    const $relay = atom({ phase: 'idle', title: '' })
    const $pct = atom(null)
    const pendingIds = new Set()
    let inFlight = false
    let lastProbeAt = 0
    let lastFailAt = 0
    // The relay whose hand-off note has not arrived yet:
    // { runtimeId, storedId, newTitle }.
    let awaiting = null

    console.log('[session-relay] loaded; threshold=' + prefs.get().threshold)

    const update = patch => {
      prefs.set({ ...prefs.get(), ...patch })
      ctx.storage.set('prefs', prefs.get())
    }

    const readRelayed = () => ctx.storage.get('relayed', {}) || {}

    const markRelayed = id => {
      const map = { ...readRelayed() }
      const cutoff = Date.now() - 30 * 864e5
      for (const k of Object.keys(map)) {
        if (!map[k] || map[k] < cutoff) {
          delete map[k]
        }
      }
      map[id] = Date.now()
      ctx.storage.set('relayed', map)
    }

    const isBusy = runtimeId => {
      const atomRef = host.state.busyBySession
      const m = (atomRef && atomRef.get && atomRef.get()) || {}
      return m[runtimeId] === true
    }

    async function precisePercent(runtimeId) {
      lastProbeAt = Date.now()
      try {
        const res = await host.request('session.context_breakdown', { session_id: runtimeId })
        if (res && typeof res.context_percent === 'number') {
          return res.context_percent
        }
      } catch (e) {
        console.log('[session-relay] breakdown failed, falling back to stream value', e)
      }
      const u = host.state.focusedUsage.get()
      return u && typeof u.context_percent === 'number' ? u.context_percent : null
    }

    /**
     * Keep the chip's number in step with the gateway. The value is only
     * accepted while the same chat is still focused, so a slow answer can
     * never paint one chat's occupancy onto another.
     */
    async function refreshPct() {
      const runtimeId = host.state.focusedSessionId && host.state.focusedSessionId.get()
      if (!runtimeId) {
        $pct.set(null)
        return null
      }
      const pct = await precisePercent(runtimeId)
      const focused = host.state.focusedSessionId && host.state.focusedSessionId.get()
      if (focused === runtimeId) {
        $pct.set(pct == null ? null : pct)
      }
      return pct
    }

    /** Give the new chat the previous title with the number incremented. */
    function applyTitle(storedId, newTitle, retry) {
      if (!newTitle || typeof host.request !== 'function') {
        return
      }
      host.request('session.title', { session_id: storedId, title: newTitle }).then(
        res => {
          const title = String((res && res.title) || '')
          console.log('[session-relay] title applied: ' + (title || newTitle))
        },
        e => {
          // A chat that went live under its runtime id may need that id instead.
          const runtimeId = host.state.focusedSessionId && host.state.focusedSessionId.get()
          if (retry && runtimeId && runtimeId !== storedId) {
            applyTitle(runtimeId, newTitle, false)
          } else {
            console.log('[session-relay] title apply failed', e)
          }
        }
      )
    }

    function watchForSession(storedId, newTitle) {
      // ~2 minute window: 24 ticks × 5 s after an initial 4 s pause.
      let left = 24
      const t0 = Date.now() / 1000
      const tick = async () => {
        try {
          const res = await host.request('session.list', { limit: 15 })
          const rows = (res && res.sessions) || []
          // Confirm by the APPEARANCE of a new session, not by its title: the
          // gateway writes the new chat's title only after its first turn
          // ends (minutes), so a title check used to time out and report a
          // false "not confirmed".
          const hit = rows.find(
            r => r && r.id !== storedId && r.source === 'desktop' && (!r.started_at || r.started_at >= t0 - 120)
          )
          if (hit) {
            applyTitle(hit.id, newTitle, true)
            $relay.set({ phase: 'done', title: newTitle })
            host.notify({ kind: 'success', message: 'Session relay ready: "' + (newTitle || 'new chat') + '"' })
            try {
              ctx.os.notify({ title: 'Session relay ready', body: newTitle || 'new chat', activate: '/' + encodeURIComponent(hit.id) })
            } catch (e) {
              console.log('[session-relay] os.notify failed', e)
            }
            return
          }
        } catch (e) {
          console.log('[session-relay] watch failed', e)
        }
        if (--left > 0) {
          ctx.setTimeout(tick, 5000)
        } else {
          $relay.set({ phase: 'idle', title: '' })
          host.notify({ kind: 'warning', message: 'Session relay: the new chat was not confirmed. Check the chat list or press ⚡ to retry.' })
        }
      }
      ctx.setTimeout(tick, 4000)
    }

    /**
     * Open a fresh chat and submit the hand-off note into it, exactly as the
     * user would: host.newChat() shows the draft, the composer sends the note.
     * The draft's composer mounts asynchronously, so the submit is retried
     * until a live surface claims it (or the window runs out).
     */
    function openAndSeed(note) {
      const a = awaiting
      awaiting = null
      if (!a) {
        return
      }
      if (typeof host.newChat !== 'function' || !host.composer || typeof host.composer.submit !== 'function') {
        $relay.set({ phase: 'idle', title: '' })
        host.notify({
          kind: 'error',
          message: 'Session relay: this Hermes Desktop build cannot open a chat from a plugin. Update Hermes Desktop.'
        })
        return
      }
      const profileAtom = host.state.focusedSessionProfile
      const profile = profileAtom && profileAtom.get ? profileAtom.get() : ''
      try {
        host.newChat(profile || null)
      } catch (e) {
        $relay.set({ phase: 'idle', title: '' })
        host.notifyError(e, 'Session relay: could not open a new chat.')
        return
      }
      const trySubmit = attempt => {
        let sent = false
        try {
          sent = host.composer.submit('new', note) === true
        } catch (e) {
          console.log('[session-relay] composer submit failed', e)
        }
        if (sent) {
          host.notify({
            kind: 'info',
            message: a.newTitle
              ? 'Session relay: "' + a.newTitle + '" starts with the hand-off note.'
              : 'Session relay: the hand-off note went to the new chat.'
          })
          watchForSession(a.storedId, a.newTitle)
          return
        }
        if (attempt < SEED_TRIES) {
          ctx.setTimeout(() => trySubmit(attempt + 1), SEED_RETRY_MS)
        } else {
          $relay.set({ phase: 'idle', title: '' })
          host.notify({
            kind: 'warning',
            message: 'Session relay: the new chat did not open. Copy the hand-off note from this chat, or press ⚡ to retry.'
          })
        }
      }
      ctx.setTimeout(() => trySubmit(0), 400)
    }

    async function relayNow(opts) {
      // The lock is taken synchronously, before the first await: parallel
      // triggers (message.complete + a chip re-render) would otherwise pass the
      // check and submit the service message several times (duplicates).
      if (inFlight) {
        return
      }
      inFlight = true
      try {
        await relayRun(opts)
      } finally {
        inFlight = false
      }
    }

    async function relayRun(opts) {
      const manual = Boolean(opts && opts.manual)
      const runtimeId = host.state.focusedSessionId.get()
      const storedId = host.state.focusedStoredSessionId.get()
      if (!runtimeId || !storedId) {
        if (manual) {
          host.notify({ kind: 'warning', message: 'Session relay: no active session.' })
        }
        return
      }
      if (awaiting) {
        if (manual) {
          host.notify({ kind: 'warning', message: 'Session relay: a hand-off is already being prepared.' })
        }
        return
      }
      if (isBusy(runtimeId)) {
        if (manual) {
          host.notify({ kind: 'warning', message: 'Session relay: the session is busy — retry after the reply.' })
        }
        return
      }
      const p = prefs.get()
      if (!manual) {
        if (!p.auto) {
          return
        }
        if (readRelayed()[storedId]) {
          return
        }
        if (pendingIds.has(storedId)) {
          return
        }
        if (Date.now() - lastFailAt < 300000) {
          return
        }
        const u = host.state.focusedUsage.get()
        const measured = $pct.get()
        const cheap =
          measured != null ? measured : u && typeof u.context_percent === 'number' ? u.context_percent : null
        if (cheap == null) {
          if (Date.now() - lastProbeAt < 60000) {
            return
          }
        } else if (cheap < p.threshold - 3) {
          return
        }
      }

      const pct = await precisePercent(runtimeId)
      if (!manual) {
        if (pct == null || pct < p.threshold) {
          return
        }
        if (readRelayed()[storedId]) {
          return
        }
      }
      if (isBusy(runtimeId)) {
        return
      }

      pendingIds.add(storedId)
      $relay.set({ phase: 'working', title: '' })
      try {
        let oldTitle = ''
        try {
          const res = await host.request('session.title', { session_id: runtimeId })
          oldTitle = String((res && res.title) || '').trim()
        } catch (e) {
          console.log('[session-relay] title read failed', e)
        }

        const newTitle = nextTitle(oldTitle)
        const msg = [
          '[Session Relay plugin — service message]',
          'Context usage in this session: ' + (pct == null ? '—' : pct + '%') + '.',
          'Write the hand-off note that will continue this work in a fresh session.',
          'Reply with the note itself as your WHOLE message: the current task, the state, what has been done, the next step, and the important details from this conversation.',
          'No tools, no preamble, no questions: the plugin opens the new chat itself and puts your reply there as its first message.'
        ].join('\n')

        awaiting = { runtimeId, storedId, newTitle }
        await host.request('prompt.submit', { session_id: runtimeId, text: msg, display_kind: 'hidden' })
        markRelayed(storedId)
        console.log('[session-relay] relay started; new title=' + (newTitle || '(auto)'))
        $relay.set({ phase: 'started', title: newTitle })
        host.notify({
          kind: 'info',
          message: newTitle ? 'Session relay: writing the hand-off for "' + newTitle + '"…' : 'Session relay: writing the hand-off…'
        })
        // A note that never arrives must not leave the chip spinning forever.
        ctx.setTimeout(() => {
          if (awaiting && awaiting.storedId === storedId) {
            awaiting = null
            $relay.set({ phase: 'idle', title: '' })
            host.notify({ kind: 'warning', message: 'Session relay: no hand-off note arrived — press ⚡ to retry.' })
          }
        }, NOTE_TIMEOUT_MS)
      } catch (e) {
        awaiting = null
        lastFailAt = Date.now()
        $relay.set({ phase: 'idle', title: '' })
        host.notifyError(e, 'Session relay: could not start.')
      } finally {
        pendingIds.delete(storedId)
      }
    }

    // The agent's reply to the service message IS the hand-off note.
    ctx.onEvent('message.complete', event => {
      const a = awaiting
      if (!a || !event) {
        return
      }
      const sessionId = event.session_id || (event.payload && event.payload.session_id) || ''
      // The note must come from THIS chat's turn: an event with no session id is not a match.
      if (sessionId !== a.runtimeId) {
        return
      }
      const payload = event.payload || {}
      if (payload.status && payload.status !== 'complete') {
        return
      }
      const note = String(payload.text || payload.content || '').trim()
      if (!note) {
        return
      }
      openAndSeed(note)
    })

    function SettingsPage() {
      const p = useValue(prefs)
      const measured = useValue($pct)
      const u = useValue(host.state.focusedUsage)
      const storedId = useValue(host.state.focusedStoredSessionId)
      const streamed = u && typeof u.context_percent === 'number' ? u.context_percent : null
      const pct = measured != null ? measured : streamed
      const marked = storedId ? Boolean(readRelayed()[storedId]) : false

      return jsxs('div', {
        children: [
          jsx(ToggleRow, {
            checked: p.auto,
            label: 'Automatic relay',
            description:
              'When the session context reaches the threshold, the agent writes a hand-off note and the plugin opens a new chat with that note as its first message (the title number is incremented).',
            onChange: on => update({ auto: on })
          }),
          jsx(ListRow, {
            title: 'Trigger threshold',
            description:
              'Hermes compacts the history at about 50%; the threshold is kept below that so the relay fires before compaction.',
            action: jsx(SegmentedControl, {
              value: String(p.threshold),
              onChange: v => update({ threshold: Number(v) }),
              options: [
                { id: '30', label: '30%' },
                { id: '40', label: '40%' },
                { id: '45', label: '45%' },
                { id: '50', label: '50%' }
              ]
            })
          }),
          jsx(ListRow, {
            title: 'Current state',
            description: pct == null ? 'The active session context has not been measured yet.' : 'Active session context: ' + pct + '%.',
            action: jsx('span', {
              className: 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-secondary)',
              children: marked ? 'relay already started' : 'relay not started yet'
            })
          }),
          jsx(ListRow, {
            title: 'Reset marks',
            description: 'Useful to retest the relay on the same session.',
            action: jsx(Button, {
              variant: 'secondary',
              size: 'sm',
              onClick: () => {
                ctx.storage.remove('relayed')
                host.notify({ kind: 'info', message: 'Session relay: marks cleared.' })
              },
              children: 'Reset'
            })
          })
        ]
      })
    }

    function Chip() {
      const p = useValue(prefs)
      const measured = useValue($pct)
      const u = useValue(host.state.focusedUsage)
      const relay = useValue($relay)
      const runtimeId = useValue(host.state.focusedSessionId)
      const storedId = useValue(host.state.focusedStoredSessionId)
      const busyMap = useValue(host.state.busyBySession)

      const streamed = u && typeof u.context_percent === 'number' ? u.context_percent : null
      const pct = measured != null ? measured : streamed
      const busyNow = Boolean(runtimeId && busyMap && busyMap[runtimeId] === true)

      useEffect(() => {
        void refreshPct()
      }, [runtimeId])

      useEffect(() => {
        void relayNow({ manual: false })
      }, [pct, runtimeId, storedId, p && p.auto, p && p.threshold, busyNow])

      const working = relay.phase === 'working' || relay.phase === 'started'
      const near = pct != null && p && pct >= p.threshold
      const label = working ? '…' : pct == null ? '—' : pct + '%'
      const tip = working
        ? 'Session relay: writing the hand-off for "' + (relay.title || 'a new chat') + '"…'
        : relay.phase === 'done'
          ? 'Session relay already created: "' + relay.title + '".'
          : pct == null
            ? 'Session relay: context not measured yet. Click to start a relay manually.'
            : 'Session relay — context ' + pct + '%. Auto threshold ' + (p ? p.threshold : '?') + '%. Click to start manually.'

      return jsx(Tip, {
        label: tip,
        children: jsxs('button', {
          type: 'button',
          className: cn(
            'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
            near ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)',
            'hover:bg-(--chrome-action-hover) hover:text-foreground'
          ),
          onClick: () => {
            haptic('tap')
            void relayNow({ manual: true })
          },
          children: [
            jsx('span', { 'aria-hidden': true, children: '⚡' }),
            jsx('span', { children: label })
          ]
        })
      })
    }

    // The chip must show a number even between turns: poll the same read-only
    // RPC the desktop's own context gauge uses.
    const tickPct = () => {
      void refreshPct()
      ctx.setTimeout(tickPct, PCT_REFRESH_MS)
    }
    ctx.setTimeout(tickPct, 1200)

    // Additional trigger: check the threshold after any turn completes.
    ctx.onEvent('message.complete', () => {
      ctx.setTimeout(() => {
        void refreshPct()
        void relayNow({ manual: false })
      }, 2500)
    })

    if (ctx.registerSettingsPage) {
      ctx.registerSettingsPage({
        id: 'main',
        title: 'Session Relay',
        icon: 'sync',
        render: SettingsPage
      })
    }

    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 145,
      render: () => jsx(Chip, {})
    })
  }
}
