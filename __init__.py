"""Session Relay for channels outside the desktop app (Telegram and other gateway channels).

Flow:
  1. In a chat the agent stores the hand-off note with the ``session_relay_handoff``
     tool: ONE file per chat under ``$HERMES_HOME/plugin-data/session-relay/``.
     The file name is derived from the chat's session id — the id the plugin
     later sees as ``old_session_id`` — and the note carries that id inside.
  2. The user sends /new (or /reset) in that chat.
  3. ``on_session_reset`` reports the session it retired; the plugin reads ONLY
     that session's note and parks it as a "carry" bound to that chat's route,
     together with the id of the session /new has just created.
  4. The next human message in that chat is rewritten by the
     ``pre_gateway_dispatch`` hook: the note becomes the opening block of that
     message. (The gateway's session authority refuses an injection into a
     session that has no committed turn yet — an empty new session cannot be
     seeded — so the note rides the first real message instead.) A note prepared
     by another chat is never carried, so a shared gateway cannot leak one
     conversation into another chat's session.
  5. The new session gets the previous title with the number incremented
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
_CARRY_DIRNAME = "carry"
# Gateways that cannot receive a rewritten inbound message: their sessions are
# driven in-process (CLI/TUI/Desktop), so the note is injected instead.
_INPROCESS_PLATFORMS = frozenset({"local", "cli", "tui"})
_CARRY_HEADER = "⚡ Передача из прошлой сессии (авто-подхват)."

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
    """Drop notes and carries no /new or message picked up within the freshness window."""
    cutoff = time.time() - _PENDING_MAX_AGE_S
    try:
        for entry in _data_dir().glob("*.json"):
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        carry_dir = _data_dir() / _CARRY_DIRNAME
        if carry_dir.is_dir():
            for entry in carry_dir.glob("*.json"):
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


def _carry_path(route: str) -> Path:
    """One carry per chat route: the hash keeps arbitrary keys file-safe."""
    digest = hashlib.sha256(route.encode("utf-8")).hexdigest()[:32]
    directory = _data_dir() / _CARRY_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{digest}.json"


def _write_carry(route: str, *, session_id: str, text: str, platform: str) -> None:
    """Park the note for the next human message in this chat (atomic replace)."""
    path = _carry_path(route)
    payload = {
        "route": route,
        "session_id": str(session_id or ""),
        "platform": platform or "",
        "text": text,
        "created_at": time.time(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    _prune_stale()


def _read_carry(route: str) -> "dict | None":
    """This chat's fresh carry, or None. Never another chat's."""
    if not route:
        return None
    path = _carry_path(route)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or str(data.get("route") or "") != route:
            logger.warning("[session-relay] carry file %s names another chat - skipping", path.name)
            return None
        age = time.time() - float(data.get("created_at") or path.stat().st_mtime)
        if age > _PENDING_MAX_AGE_S:
            path.unlink(missing_ok=True)
            return None
        return data if str(data.get("text") or "").strip() else None
    except Exception:
        logger.debug("[session-relay] failed to read the chat's carry", exc_info=True)
        return None


def _remove_carry(route: str) -> None:
    try:
        _carry_path(route).unlink(missing_ok=True)
    except Exception:
        logger.debug("[session-relay] failed to remove the carry", exc_info=True)


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

            if (platform or "").lower() in _INPROCESS_PLATFORMS:
                # A local session has no inbound message to rewrite: inject the note as its own turn.
                accepted = False
                try:
                    accepted = bool(ctx.inject_message(text, role="user", session_key=route))
                except Exception:
                    logger.warning("[session-relay] inject_message failed", exc_info=True)
                logger.info("[session-relay] hand-off injected: session=%s platform=%s accepted=%s",
                            retired, platform or "?", accepted)
                if accepted:
                    _remove_pending(retired)
                    _apply_incremented_title(old_session_id, new_session_id)
                return

            # A messaging chat cannot be seeded while its new session has no committed turn (the
            # session authority refuses with not_found), so park the note for the chat's next
            # human message; pre_gateway_dispatch rewrites that message to open with the note.
            _write_carry(route, session_id=fresh or retired, text=text, platform=platform or "")
            _remove_pending(retired)
            _apply_incremented_title(old_session_id, new_session_id)
            logger.info("[session-relay] hand-off parked for the next message in this chat: "
                        "session=%s platform=%s", retired, platform or "?")
        except Exception:
            logger.debug("[session-relay] on_session_reset failed", exc_info=True)

    def _on_pre_gateway_dispatch(*, event=None, gateway=None, session_store=None, **_kw):
        """Open the first human message after /new with this chat's parked note."""
        try:
            text = str(getattr(event, "text", "") or "")
            if not text.strip() or text.lstrip().startswith("/"):
                return None  # commands (a second /new, /stop, ...) pass through untouched
            source = getattr(event, "source", None)
            if source is None:
                return None
            platform = str(getattr(getattr(source, "platform", None), "value", "") or "")
            if platform in _INPROCESS_PLATFORMS:
                return None
            resolver = getattr(gateway, "_session_key_for_source", None)
            route = str(resolver(source) or "").strip() if callable(resolver) else ""
            carry = _read_carry(route)
            if not carry:
                return None
            expected = str(carry.get("session_id") or "")
            if session_store is not None:
                entry = session_store.lookup_by_session_key(route)
                current = str(getattr(entry, "session_id", "") or "") if entry is not None else ""
                if not current or (expected and current != expected):
                    # The chat has moved on (another /new, a restored session): drop the stale note.
                    _remove_carry(route)
                    return None
            note = str(carry.get("text") or "").strip()
            _remove_carry(route)
            if not note:
                return None
            logger.info("[session-relay] hand-off attached to the next message in this chat: route=%s", route)
            return {"action": "rewrite", "text": f"{_CARRY_HEADER}\n\n{note}\n\n---\n\n{text}"}
        except Exception:
            logger.debug("[session-relay] pre_gateway_dispatch failed", exc_info=True)
            return None

    ctx.register_tool(
        name=_TOOL_NAME,
        toolset="session_relay",
        schema=_TOOL_SCHEMA,
        handler=_tool_handoff,
        description="Store the hand-off note this chat's next /new must start with.",
        emoji="⚡",
    )
    ctx.register_hook("on_session_reset", _on_session_reset)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    logger.info("[session-relay] plugin loaded (hand-offs: %s, tool: %s)", _data_dir(), _TOOL_NAME)
