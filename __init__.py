"""Session Relay for channels outside the desktop app (Telegram and other gateway channels).

Flow:
  1. In the conversation the agent (on the word "relay"/"handoff") writes the
     hand-off summary to <this folder>/pending.json:
         {"platform": "telegram", "text": "<hand-off>"}
  2. The user sends /new (or /reset).
  3. The plugin remembers the session_key on pre_command, and after the reset
     (on_session_reset) injects the hand-off as the first message of the new
     session (ctx.inject_message) and removes pending.json.
  4. The new session gets the previous title with the number incremented
     ("Bugfix-6" -> "Bugfix-7") unless it already has a title.

Requires in config.yaml (profile default):
  plugins:
    enabled: [session-relay-gw]
    entries:
      session-relay-gw:
        allow_gateway_injection: true

Disable: remove it from plugins.enabled and restart the gateway.
Everything is best-effort: a plugin error never breaks message handling.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent
_PENDING_FILE = _PLUGIN_DIR / "pending.json"
_PENDING_MAX_AGE_S = 24 * 3600  # a prepared hand-off lives for one day
_TITLE_LIMIT = 40


def _next_title(title: str) -> str:
    """"Bugfix-6" -> "Bugfix-7"; without a number -> "...-2"."""
    t = (title or "").strip()
    if not t:
        return ""
    m = re.match(r"^(.*?)(\d+)(\D*)$", t, re.S)
    if not m:
        out = t + "-2"
    else:
        old_num = m.group(2)
        num = str(int(old_num) + 1)
        if len(old_num) > 1 and old_num.startswith("0"):
            num = num.zfill(len(old_num))
        out = m.group(1) + num + m.group(3)
    return out[:_TITLE_LIMIT]


def _read_pending_text(platform: str):
    """A fresh prepared hand-off for this platform, or None."""
    try:
        if not _PENDING_FILE.is_file():
            return None
        age = time.time() - _PENDING_FILE.stat().st_mtime
        if age > _PENDING_MAX_AGE_S:
            logger.info("[session-relay-gw] pending.json is stale (%.0f h) - skipping", age / 3600.0)
            return None
        data = json.loads(_PENDING_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        want = str(data.get("platform") or "").strip().lower()
        have = str(platform or "").strip().lower()
        if want and have and want != have:
            logger.info("[session-relay-gw] pending is for platform %r but the reset is on %r - skipping", want, have)
            return None
        text = str(data.get("text") or "").strip()
        return text or None
    except Exception:
        logger.debug("[session-relay-gw] failed to read pending.json", exc_info=True)
        return None


def _remove_pending() -> None:
    try:
        _PENDING_FILE.unlink(missing_ok=True)
    except Exception:
        logger.debug("[session-relay-gw] failed to remove pending.json", exc_info=True)


def _apply_incremented_title(old_session_id, new_session_id) -> None:
    """Give the new session the previous title with the number incremented."""
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        old = db.get_session_title(old_session_id) if old_session_id else None
        if not old:
            return
        cur = db.get_session_title(new_session_id) if new_session_id else None
        if cur:
            return  # already named (for example by the user: "/new My title")
        new_title = _next_title(old)
        if new_title:
            db.set_session_title(new_session_id, new_title)
            logger.info("[session-relay-gw] session title: %r -> %r", old, new_title)
    except Exception:
        logger.debug("[session-relay-gw] failed to set the session title", exc_info=True)


def register(ctx) -> None:
    """Called once by the plugin loader."""
    state = {"session_key": None, "platform": ""}

    def _on_pre_command(*, surface="", command="", alias_used="", args_raw="",
                        session_key=None, platform=None, **_kw) -> None:
        """Remember the session_key on /new or /reset (before the reset)."""
        try:
            cmd = str(command or "").strip().lower().lstrip("/")
            if cmd in ("new", "reset") and session_key:
                state["session_key"] = str(session_key)
                state["platform"] = str(platform or "")
                logger.info("[session-relay-gw] /%s: session_key=%s platform=%s",
                            cmd, state["session_key"], state["platform"])
        except Exception:
            logger.debug("[session-relay-gw] pre_command failed", exc_info=True)

    def _on_session_reset(*, session_id=None, reason="", platform="",
                          old_session_id=None, new_session_id=None, **_kw) -> None:
        """After /new: inject the prepared hand-off into the new session."""
        try:
            key = state["session_key"]
            plat = state["platform"] or platform
            state["session_key"] = None
            state["platform"] = ""

            text = _read_pending_text(plat)
            if not text:
                return
            if not key:
                logger.info("[session-relay-gw] hand-off exists but no session_key was captured "
                            "(reset while the agent was busy?) - keeping pending.json")
                return

            accepted = False
            try:
                accepted = bool(ctx.inject_message(text, role="user", session_key=key))
            except Exception:
                logger.warning("[session-relay-gw] inject_message failed", exc_info=True)

            logger.info("[session-relay-gw] hand-off after /new: session=%s platform=%s accepted=%s",
                        key, plat, accepted)
            if accepted:
                _remove_pending()
                _apply_incremented_title(old_session_id, new_session_id)
        except Exception:
            logger.debug("[session-relay-gw] on_session_reset failed", exc_info=True)

    ctx.register_hook("pre_command", _on_pre_command)
    ctx.register_hook("on_session_reset", _on_session_reset)
    logger.info("[session-relay-gw] plugin loaded (pending: %s)", _PENDING_FILE)
