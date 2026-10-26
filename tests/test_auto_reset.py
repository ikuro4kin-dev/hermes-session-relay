"""Offline check for the automatic reset: the gateway's /new funnel is called on its loop
with a command-free event (no title, no text).

Run:  python tests/test_auto_reset.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERMES = Path(r"C:\Users\ikuro\AppData\Local\hermes\hermes-agent")
os.environ["HERMES_HOME"] = str(tempfile.mkdtemp(prefix="relayresetauto_"))
sys.path.insert(0, str(HERMES))

spec = importlib.util.spec_from_file_location("session_relay_auto_reset", REPO / "__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ok = []


def check(name: str, value: bool) -> None:
    ok.append((name, bool(value)))
    print(("PASS " if value else "FAIL ") + name)


class Origin:
    platform = type("P", (), {"value": "telegram"})()


class Entry:
    session_key = "agent:main:telegram:dm:1"
    session_id = "sid-old"
    origin = Origin()


seen = {}


class Runner:
    def __init__(self, loop):
        self._gateway_loop = loop

    def _restored_source(self, entry):
        seen["restored_for"] = entry.session_key
        return "SOURCE"

    async def _handle_reset_command(self, event):
        seen["event"] = event
        seen["source"] = event.source
        seen["text"] = event.text
        seen["args"] = event.get_command_args()


loop = asyncio.new_event_loop()
thread = threading.Thread(target=loop.run_forever, daemon=True)
thread.start()
runner = Runner(loop)

scheduled = mod._schedule_reset(runner, Entry())
check("reset was scheduled and awaited", scheduled is True)
check("the funnel was called", "event" in seen)
check("the funnel got the chat's own source", seen.get("source") == "SOURCE")
check("the event carries no text", seen.get("text") == "")
check("the event yields no /new title", seen.get("args") == "")
check("source restored from the routed entry", seen.get("restored_for") == Entry.session_key)

# no loop -> refuses instead of pretending
class NoLoop:
    _gateway_loop = None

    def _restored_source(self, entry):
        return "SOURCE"


check("no loop means no reset", mod._schedule_reset(NoLoop(), Entry()) is False)

# a funnel that raises -> reported as failure, not a crash
class Broken(Runner):
    async def _handle_reset_command(self, event):
        raise RuntimeError("boom")


check("a failing funnel is reported", mod._schedule_reset(Broken(loop), Entry()) is False)

# --- session title numbering must survive the length limit ------------------
check("short title gets -2", mod._next_title("Bugfix") == "Bugfix-2")
check("numbered title increments", mod._next_title("Bugfix-6") == "Bugfix-7")
check("zero padding is kept", mod._next_title("Step-09") == "Step-10")
_long_old = "Перевод комментариев к релизу hermes-agent"
_long_new = mod._next_title(_long_old)
check("a long title keeps its number", _long_new.endswith("-2") and len(_long_new) <= mod._TITLE_LIMIT)
check(
    "only the base is trimmed",
    _long_new == _long_old[: mod._TITLE_LIMIT - 2] + "-2",
)

loop.call_soon_threadsafe(loop.stop)
failed = [n for n, good in ok if not good]
print()
print(f"{len(ok)} checks, {len(failed)} failed")
raise SystemExit(1 if failed else 0)
