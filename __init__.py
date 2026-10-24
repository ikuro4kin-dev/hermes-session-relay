"""Session Relay for channels outside the desktop app (Telegram and other gateway channels).

Flow:
  1. In a chat the hand-off note is stored with the ``session_relay_handoff``
     tool: ONE file per chat under ``$HERMES_HOME/plugin-data/session-relay/``.
     The file name is derived from the chat's session_key and the note carries
     that key inside.
  2. The user sends /new (or /reset) in that chat.
  3. ``on_session_reset`` resolves the resetting chat's session_key from the
     gateway routing index and reads ONLY that chat's note. It is injected as the
     first message of the new session (``ctx.inject_message``) and the file is
     deleted. A note prepared by another chat is never injected, so a shared
     gateway cannot leak one conversation into another chat's session.
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
_PENDING_MAX_AGE_S = 24 * 3600  # a prepared hand-off lives for one day
_TITLE_LIMIT = 40


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


def _pending_path(session_key: str) -> Path:
    """One file per chat: the hash keeps arbitrary session keys file-safe."""
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]
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


def _write_pending(session_key: str, text: str, platform: str) -> None:
    """Store this chat's hand-off (atomic replace, one file per session_key)."""
    path = _pending_path(session_key)
    payload = {
        "session_key": session_key,
        "platform": platform,
        "text": text,
        "created_at": time.time(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    _prune_stale()


def _read_pending(session_key: str) -> "str | None":
    """The fresh hand-off THIS chat prepared, or None. Never another chat's."""
    path = _pending_path(session_key)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if str(data.get("session_key") or "") != session_key:
            logger.warning("[session-relay] hand-off file %s names another chat - skipping", path.name)
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


def _remove_pending(session_key: str) -> None:
    try:
        _pending_path(session_key).unlink(missing_ok=True)
    except Exception:
        logger.debug("[session-relay] failed to remove the chat's hand-off", exc_info=True)


def _reset_session_key(old_session_id, new_session_id) -> str:
    """The route key of the chat ``/new`` just reset, "" when it can't be read.

    ``on_session_reset`` fires without a session_key (only session ids and the
    platform), so the key is read back from the gateway routing index: the row
    owning the reset session IS this chat's row. The old id is tried first — the
    row may still point at it if the persistence write has not landed yet.
    """
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        for sid in (old_session_id, new_session_id):
            if not sid:
                continue
            entry = db.gateway_routing_entry_for_session(str(sid))
            key = str((entry or {}).get("session_key") or "").strip()
            if key:
                return key
    except Exception:
        logger.debug("[session-relay] could not resolve the resetting chat", exc_info=True)
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


def register(ctx) -> None:
    """Called once by the plugin loader."""

    def _on_session_reset(*, session_id=None, reason="", platform="",
                          old_session_id=None, new_session_id=None, **_kw) -> None:
        """After /new: inject THAT chat's prepared hand-off into the new session."""
        try:
            key = _reset_session_key(old_session_id, new_session_id)
            if not key:
                logger.info("[session-relay] /new on %s: cannot tell which chat was reset - "
                            "no hand-off injected", platform or "?")
                return
            text = _read_pending(key)
            if not text:
                return

            accepted = False
            try:
                accepted = bool(ctx.inject_message(text, role="user", session_key=key))
            except Exception:
                logger.warning("[session-relay] inject_message failed", exc_info=True)

            logger.info("[session-relay] hand-off after /new: session=%s platform=%s accepted=%s",
                        key, platform or "?", accepted)
            if accepted:
                _remove_pending(key)
                _apply_incremented_title(old_session_id, new_session_id)
        except Exception:
            logger.debug("[session-relay] on_session_reset failed", exc_info=True)

    ctx.register_hook("on_session_reset", _on_session_reset)
    logger.info("[session-relay] plugin loaded (hand-offs: %s)", _data_dir())
