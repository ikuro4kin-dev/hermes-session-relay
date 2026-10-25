"""Session Relay for channels outside the desktop app (Telegram and other gateway channels).

Flow:
  1. In a chat the agent stores the hand-off note with the ``session_relay_handoff``
     tool: ONE file per chat under ``$HERMES_HOME/plugin-data/session-relay/``.
     The file name is derived from the chat's session id — the id the plugin
     later sees as ``old_session_id`` — and the note carries that id inside.
  2. The user sends /new (or /reset) in that chat.
  3. ``on_session_reset`` reports the session it retired; the plugin reads ONLY
     that session's note, injects it as the first message of the new session
     (``ctx.inject_message``) and deletes the file. A note prepared by another
     chat is never injected, so a shared gateway cannot leak one conversation
     into another chat's session.
  4. The new session gets the previous title with the number incremented
     ("Bugfix-6" -> "Bugfix-7") unless it already has a title.

Requires in config.yaml (profile default):
  plugins:
    enabled: [session-relay]
    entries:
      session-relay:
        allow_gateway_injection: true

Disable: remove it from plugins.enabled and restart the gateway.
Everything is best-effort: a plugin error never breaks message handling.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PLUGIN_ID = "session-relay"
_TOOL_NAME = "session_relay_handoff"
_PENDING_MAX_AGE_S = 24 * 3600  # a prepared hand-off lives for one day
_TITLE_LIMIT = 40

_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": (
        "Store the hand-off note that the NEXT /new in this chat must start with. Use it when the "
        "user asks for a relay/hand-off. The note is bound to this chat and is injected only into "
        "this chat's next session after /new."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The hand-off note itself: the current task, the state, what has been done, the "
                    "next step, and the important details from this conversation."
                ),
            }
        },
        "required": ["text"],
    },
}


def _next_title(title: str) -> str:
    """\"Bugfix-6\" -> \"Bugfix-7\"; without a number -> \"...-2\"."""
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


def _data_dir() -> Path:
    """``$HERMES_HOME/plugin-data/session-relay/`` — created on first use.

    Never the plugin install folder: ``hermes plugins update`` git-pulls that and
    ``remove`` deletes it, so user data must not live there.
    """
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(_PLUGIN_ID)


def _pending_path(session_id: str) -> Path:
    """One file per chat: the hash keeps arbitrary ids file-safe."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _data_dir() / f"{digest}.json"


def _prune_stale() -> None:
    """Drop notes no /new picked up within the freshness window."""
    cutoff = time.time() - _PENDING_MAX_AGE_S
    try:
        for entry in _data_dir().glob("*.json"):
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
    except Exception:
        logger.debug("[session-relay] failed to prune stale hand-offs", exc_info=True)


def _write_pending(session_id: str, text: str) -> None:
    """Store this chat's hand-off (atomic replace, one file per session id)."""
    path = _pending_path(session_id)
    payload = {
        "session_id": session_id,
        "text": text,
        "created_at": time.time(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    _prune_stale()


def _read_pending(session_id: str) -> "str | None":
    """The fresh hand-off THIS chat prepared, or None. Never another chat's."""
    path = _pending_path(session_id)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if str(data.get("session_id") or "") != session_id:
            logger.warning("[session-relay] hand-off file %s names another session - skipping", path.name)
            return None
        age = time.time() - float(data.get("created_at") or path.stat().st_mtime)
        if age > _PENDING_MAX_AGE_S:
            logger.info("[session-relay] this chat's hand-off is stale (%.0f h) - dropping", age / 3600.0)
            path.unlink(missing_ok=True)
            return None
        text = str(data.get("text") or "").strip()
        return text or None
    except Exception:
        logger.debug("[session-relay] failed to read the chat's hand-off", exc_info=True)
        return None


def _remove_pending(session_id: str) -> None:
    try:
        _pending_path(session_id).unlink(missing_ok=True)
    except Exception:
        logger.debug("[session-relay] failed to remove the chat's hand-off", exc_info=True)


def _routing_key_for_session(session_id: str) -> str:
    """The chat's routing key (what ``inject_message`` targets), "" when unknown.

    ``on_session_reset`` reports the retired session id but not its routing key,
    so the key is read back from the gateway routing index (state.db
    ``gateway_routing``): the row owning this session IS the chat's row.
    """
    try:
        from hermes_state import SessionDB

        entry = SessionDB().gateway_routing_entry_for_session(str(session_id))
        return str((entry or {}).get("session_key") or "").strip()
    except Exception:
        logger.debug("[session-relay] could not resolve the routing key", exc_info=True)
    return ""


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
            logger.info("[session-relay] session title: %r -> %r", old, new_title)
    except Exception:
        logger.debug("[session-relay] failed to set the session title", exc_info=True)


def _tool_handoff(args: dict, *, session_id: str = "", **_kwargs) -> str:
    """``session_relay_handoff(text=...)``: store the note for THIS chat only."""
    try:
        text = str((args or {}).get("text") or "").strip()
        if not text:
            return json.dumps({"ok": False, "error": "`text` is required: pass the hand-off note itself."})
        sid = str(session_id or "").strip()
        if not sid:
            return json.dumps(
                {"ok": False, "error": "This turn has no session, so the hand-off has nowhere to go."}
            )
        _write_pending(sid, text)
        logger.info("[session-relay] hand-off stored for session %s (%d chars)", sid, len(text))
        return json.dumps(
            {
                "ok": True,
                "session_id": sid,
                "expires_in_hours": _PENDING_MAX_AGE_S // 3600,
                "next": "Ask the user to send /new in THIS chat; the note becomes the first message of the new session.",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.debug("[session-relay] could not store the hand-off", exc_info=True)
        return json.dumps({"ok": False, "error": f"could not store the hand-off: {exc}"})


def register(ctx) -> None:
    """Called once by the plugin loader."""

    def _on_session_reset(*, session_id=None, reason="", platform="",
                          old_session_id=None, new_session_id=None, **_kw) -> None:
        """After /new: inject THAT chat's prepared hand-off into the new session."""
        try:
            retired = str(old_session_id or "").strip()
            if not retired:
                logger.info("[session-relay] /new on %s with no retired session - nothing to inject",
                            platform or "?")
                return
            text = _read_pending(retired)
            if not text:
                return
            fresh = str(new_session_id or "").strip()
            # /new has already moved this chat's routing row to the new session id, so the
            # retired id no longer owns a row: resolve by the successor first, then fall back.
            route = (_routing_key_for_session(fresh) if fresh else "") or _routing_key_for_session(retired)
            if not route:
                logger.info("[session-relay] hand-off for session %s is kept: its chat has no "
                            "routing key recorded", retired)
                return

            accepted = False
            try:
                accepted = bool(ctx.inject_message(text, role="user", session_key=route))
            except Exception:
                logger.warning("[session-relay] inject_message failed", exc_info=True)

            logger.info("[session-relay] hand-off after /new: session=%s platform=%s accepted=%s",
                        retired, platform or "?", accepted)
            if accepted:
                _remove_pending(retired)
                _apply_incremented_title(old_session_id, new_session_id)
        except Exception:
            logger.debug("[session-relay] on_session_reset failed", exc_info=True)

    ctx.register_tool(
        name=_TOOL_NAME,
        toolset="session_relay",
        schema=_TOOL_SCHEMA,
        handler=_tool_handoff,
        description="Store the hand-off note this chat's next /new must start with.",
        emoji="⚡",
    )
    ctx.register_hook("on_session_reset", _on_session_reset)
    logger.info("[session-relay] plugin loaded (hand-offs: %s, tool: %s)", _data_dir(), _TOOL_NAME)
