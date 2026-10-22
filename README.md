# Session Relay — a Hermes Agent plugin

![Session Relay: hand off a long-running chat to a fresh session](assets/banner.png)

**Hand your long-running chat over to a fresh session before the context window fills up.**

Hermes compacts a conversation when its context reaches ~50% of the model window. Compaction keeps a summary and drops details; after two or three compactions a long working session turns into a soup of half-remembered facts and duplicated messages. Session Relay solves the same problem the other way round: as the context approaches the threshold, the agent writes a hand-off note and opens a **new chat** that starts with that note — while the old chat stays untouched and readable.

## How it works

1. The desktop half watches the focused session's context usage.
2. When usage crosses the threshold (45% by default) and the current turn has finished, it submits a **hidden service message**: the agent writes a hand-off (current task, state, what was done, next step, important details) and calls `start_chat`.
3. The new chat is named after the current one with the number incremented — `Bugfix-6` → `Bugfix-7`.
4. A toast and an OS notification announce the new chat; clicking the notification opens it.
5. The ⚡ chip in the status bar starts a relay manually, below the threshold too.

Nothing is deleted: the previous session remains in the sidebar as the full record.

On channels outside the desktop app (Telegram and other messaging platforms) the flow is manual: the agent prepares the hand-off, you send `/new`, and the gateway half injects it as the first message of the new session.

## Requirements

- **Hermes Agent 0.22.0 or newer**, with the Desktop app (`apps/desktop`) and/or the gateway.
- The `start_chat` tool must be available to the agent. It ships with Hermes but is not listed in every toolset by default; add it to the desktop/CLI toolset in `config.yaml` if a relay reports that `start_chat` is unknown:

  ```yaml
  platform_toolsets:
    cli:
      - ...existing tools...
      - start_chat
  ```

## Package layout

This is a **unified package**: one plugin, two halves.

```
session-relay/
├── plugin.yaml      # manifest (name, version, declared hooks)
├── __init__.py      # gateway half: injects a prepared hand-off after /new
└── desktop/
    └── plugin.js    # desktop half: context meter, threshold, service message
```

## Install

**From Git (recommended):**

```bash
hermes plugins install https://github.com/ikuro4kin-dev/hermes-session-relay
```

This puts the package in `$HERMES_HOME/plugins/session-relay/`. Then:

1. **Gateway half (optional)** — enable it in `config.yaml`:

   ```yaml
   plugins:
     enabled: [session-relay]
     entries:
       session-relay:
         allow_gateway_injection: true
   ```

2. **Desktop half** — open Hermes Desktop → **Capabilities → Plugins → Rescan**, then enable **Session Relay**. Both halves are opt-in and default to off.
3. Restart the gateway after enabling the Python half (`hermes gateway restart`).

**Desktop-only, without the gateway half:** copy `desktop/plugin.js` to
`$HERMES_HOME/desktop-plugins/session-relay/plugin.js` (Windows: `%LOCALAPPDATA%\hermes\desktop-plugins\session-relay\plugin.js`) and enable the plugin in **Settings → Plugins**. No build step, no dependencies: it is a single ESM file.

## Settings (desktop)

| Setting | Meaning |
|---|---|
| **Automatic relay** | Fire the hand-off automatically when the threshold is reached. |
| **Trigger threshold** | 30 / 40 / 45 / 50 %. The default is 45 % — below the point where Hermes compacts on its own (~50 %). |
| **Reset marks** | Forget that a relay already ran for the current session (useful for testing). |

## Using the gateway half (Telegram and other channels)

1. Ask the agent for a relay / hand-off for the current conversation.
2. Send `/new` (or `/reset`).
3. The plugin injects the prepared hand-off as the first user message of the new session and gives it the previous title with the number incremented.
4. A prepared hand-off is valid for 24 hours; if it is older, it is ignored.

These channels are manual by design: the plugin cannot watch a context meter from the gateway side.

## Disclosure

What the plugin does with your data and tokens:

- Each relay submits **one service message to the agent**, which starts a normal agent turn — this consumes model tokens like any other turn.
- The hand-off text is produced by the agent from the current conversation and passed to `start_chat`; it becomes the first user message of the new session.
- The desktop half keeps two keys in the plugin's own storage: `prefs` (your settings) and `relayed` (session ids already relayed, pruned after 30 days).
- The gateway half writes `pending.json` inside its own folder (hand-off text, platform name) and deletes it after the message is injected; entries older than 24 hours are ignored.
- No network calls, no telemetry, no shell access, no access to files outside its own folder. Everything goes through Hermes's own plugin APIs.

## Limitations

- Both halves rely on Hermes-internal plugin APIs (`session.context_breakdown`, `prompt.submit`, `start_chat`, and the `pre_command` / `on_session_reset` hooks). A future Hermes release may change them — the plugin would need an update.
- The automatic trigger exists only for the desktop app; Telegram and other gateway channels use the manual flow described above.
- The relay runs only when the session is idle (it never interrupts a running turn).

## License

MIT — see [LICENSE](LICENSE).
