"""Offline checks for the automatic hand-off watchdog (no gateway, no model).

Run:  python tests/test_auto_handoff.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERMES = Path(r"C:\Users\ikuro\AppData\Local\hermes\hermes-agent")
HOME = Path(tempfile.mkdtemp(prefix="relayauto_"))
os.environ["HERMES_HOME"] = str(HOME)
sys.path.insert(0, str(HERMES))

spec = importlib.util.spec_from_file_location("session_relay_auto", REPO / "__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ok = []


def check(name: str, value: bool, detail: str = "") -> None:
    ok.append((name, bool(value)))
    print(("PASS " if value else "FAIL ") + name + (f"  [{detail}]" if detail and not value else ""))


class Origin:
    def __init__(self, platform: str) -> None:
        self.platform = type("P", (), {"value": platform})()


class Entry:
    def __init__(self, key: str, sid: str, platform: str) -> None:
        self.session_key = key
        self.session_id = sid
        self.origin = Origin(platform)


class Store:
    def __init__(self, entries) -> None:
        self._entries = {e.session_key: e for e in entries}

    def list_sessions(self):
        return list(self._entries.values())

    def lookup_by_session_key(self, key):
        return self._entries.get(key)


class Runner:
    def __init__(self, store) -> None:
        self.session_store = store
        self._running_agents = {}


class Ctx:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.injected = []

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def inject_message(self, text, role="user", session_key=None):
        self.injected.append((session_key, text))
        return True


def write_log(path: Path, sid: str, platform: str, tokens: int, age_s: int) -> None:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_s))
    lines = [
        f"{when},000 INFO [{sid}] agent.turn_context: conversation turn: session={sid} model=test model "
        f"provider=test platform={platform} history=10 msg='hi'",
        f"{when},500 INFO [{sid}] agent.conversation_loop: API call #3: model=testmodel provider=test "
        f"in={tokens} out=10 total={tokens + 10} latency=1.0s cache=0/{tokens} (0%) id=x",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fresh_state() -> None:
    p = mod._auto_state_path()
    if p.exists():
        p.unlink()


def run(cfg, store, log_path, schedule=None, last_word=None):
    mod._gateway_runner = lambda: Runner(store)
    mod._agent_log_path = lambda: log_path
    mod._context_limit = lambda model: 1000  # no network, 1k window
    mod._last_user_word_ts = last_word or (lambda sid: time.time())
    calls = []

    def fake_schedule(runner, entry):
        calls.append(entry.session_key)
        return True

    mod._schedule_reset = schedule or fake_schedule
    ctx = Ctx(cfg)
    mod._auto_tick(ctx)
    return ctx, calls


BASE = {"auto_handoff": True, "auto_threshold_percent": 45.0, "auto_min_idle_seconds": 60.0,
        "auto_channels": ["telegram"]}
log = HOME / "agent.log"

# 1. the switch is off -> nothing happens
fresh_state()
write_log(log, "sid1", "telegram", 800, 600)
ctx, calls = run({**BASE, "auto_handoff": False}, Store([Entry("route1", "sid1", "telegram")]), log)
check("disabled switch injects nothing", ctx.injected == [])

# 2. over the threshold and idle -> one trigger for this chat
fresh_state()
ctx, calls = run(BASE, Store([Entry("route1", "sid1", "telegram")]), log)
check("trigger injected once", len(ctx.injected) == 1 and ctx.injected[0][0] == "route1")
check("trigger carries the percentage", "%" in ctx.injected[0][1] if ctx.injected else False)
state = json.loads(mod._auto_state_path().read_text(encoding="utf-8"))
check("state marks the session as triggered", state.get("sid1", {}).get("stage") == "triggered")

# 3. a second tick must not inject again
ctx2, calls = run(BASE, Store([Entry("route1", "sid1", "telegram")]), log)
check("no second trigger for the same session", ctx2.injected == [])

# 4. no note yet -> no reset
check("no reset without the note", calls == [])

# 5. the agent wrote the note -> reset is scheduled, then cleaned up when the route moves
note = mod._pending_path("sid1")
note.parent.mkdir(parents=True, exist_ok=True)
note.write_text(json.dumps({"session_id": "sid1", "text": "hand-off", "created_at": time.time()}),
                encoding="utf-8")
store = Store([Entry("route1", "sid1", "telegram")])
ctx3, calls = run(BASE, store, log)
check("reset scheduled when the note exists", calls == ["route1"])
state = json.loads(mod._auto_state_path().read_text(encoding="utf-8"))
check("state marks the session as resetting", state.get("sid1", {}).get("stage") == "resetting")
store._entries["route1"] = Entry("route1", "sid2", "telegram")
ctx4, calls4 = run(BASE, store, log)
state = json.loads(mod._auto_state_path().read_text(encoding="utf-8"))
check("state cleared after the route moved", "sid1" not in state)

# 6. below the threshold -> nothing
fresh_state()
write_log(log, "sid2", "telegram", 300, 600)
ctx5, calls5 = run(BASE, Store([Entry("route1", "sid2", "telegram")]), log)
check("under the threshold injects nothing", ctx5.injected == [])

# 7. an active chat is left alone (last line seconds ago)
fresh_state()
write_log(log, "sid3", "telegram", 800, 5)
ctx6, calls6 = run(BASE, Store([Entry("route1", "sid3", "telegram")]), log)
check("a chat that just spoke is left alone", ctx6.injected == [])

# 8. a turn in flight -> nothing
fresh_state()
write_log(log, "sid4", "telegram", 800, 600)
runner = Runner(Store([Entry("route1", "sid4", "telegram")]))
runner._running_agents = {"route1": object()}
mod._gateway_runner = lambda: runner
mod._agent_log_path = lambda: log
mod._context_limit = lambda model: 1000
ctx7 = Ctx(BASE)
mod._auto_tick(ctx7)
check("a busy session is left alone", ctx7.injected == [])

# 9. cross-channel guard: the desktop drove this session last
fresh_state()
write_log(log, "sid5", "desktop", 800, 600)
ctx8, calls8 = run(BASE, Store([Entry("route1", "sid5", "telegram")]), log)
check("a session the desktop drove is left alone", ctx8.injected == [])

# 10. only configured channels
fresh_state()
write_log(log, "sid6", "api_server", 800, 600)
ctx9, calls9 = run({**BASE, "auto_channels": ["telegram"]}, Store([Entry("route1", "sid6", "api_server")]), log)
check("channels outside auto_channels are left alone", ctx9.injected == [])

# 11. a stuck trigger expires
fresh_state()
mod._save_auto_state({"sid7": {"stage": "triggered", "ts": time.time() - 3600, "route": "route1"}})
write_log(log, "sid7", "telegram", 100, 600)
ctx10, calls10 = run(BASE, Store([Entry("route1", "sid7", "telegram")]), log)
state = json.loads(mod._auto_state_path().read_text(encoding="utf-8"))
check("a trigger that never produced a note expires", "sid7" not in state)

# 12. the chat spoke after the note -> the stale note is dropped and nothing is reset
fresh_state()
note8 = mod._pending_path("sid8")
note8.parent.mkdir(parents=True, exist_ok=True)
note8.write_text(json.dumps({"session_id": "sid8", "text": "old", "created_at": time.time()}),
                 encoding="utf-8")
os.utime(note8, (time.time() - 300, time.time() - 300))
mod._save_auto_state({"sid8": {"stage": "triggered", "ts": time.time() - 60, "route": "route1"}})
write_log(log, "sid8", "telegram", 800, 5)  # the chat spoke 5 seconds ago, after the note
ctx11, calls11 = run(BASE, Store([Entry("route1", "sid8", "telegram")]), log)
state = json.loads(mod._auto_state_path().read_text(encoding="utf-8"))
check("a note older than the chat's last words is dropped", not note8.exists())
check("nothing is reset with a stale note", calls11 == [] and ctx11.injected == [])
check("the chat goes on a cooldown instead of re-triggering",
      state.get("sid8", {}).get("stage") == "cooldown")

# 12b. the agent's own reply after the note must NOT cancel the relay (live bug, 29.10)
fresh_state()
note9 = mod._pending_path("sid9")
note9.parent.mkdir(parents=True, exist_ok=True)
note9.write_text(json.dumps({"session_id": "sid9", "text": "fresh", "created_at": time.time()}),
                 encoding="utf-8")
os.utime(note9, (time.time() - 30, time.time() - 30))
mod._save_auto_state({"sid9": {"stage": "triggered", "ts": time.time() - 60, "route": "route1"}})
write_log(log, "sid9", "telegram", 800, 2)  # the service turn and the agent's reply landed after
ctx12, calls12 = run(BASE, Store([Entry("route1", "sid9", "telegram")]), log,
                     last_word=lambda sid: time.time() - 600)  # the human wrote before the note
check("a note survives the agent's own reply", calls12 == ["route1"])

# 12c. a cancelled chat is left alone for the cooldown window, then may relay again
fresh_state()
write_log(log, "sid10", "telegram", 800, 600)
mod._save_auto_state({"sid10": {"stage": "cooldown", "ts": time.time() - 60, "route": "route1"}})
ctx13, calls13 = run(BASE, Store([Entry("route1", "sid10", "telegram")]), log)
check("cooldown blocks a new trigger", ctx13.injected == [])
mod._save_auto_state({"sid10": {"stage": "cooldown",
                                "ts": time.time() - (mod._AUTO_CANCEL_COOLDOWN_S + 10),
                                "route": "route1"}})
ctx14, calls14 = run(BASE, Store([Entry("route1", "sid10", "telegram")]), log)
check("cooldown expires and the chat can relay again", len(ctx14.injected) == 1)

# 18. web channel (api_server): a wake turn is started, nothing is injected and nothing reset
fresh_state()
write_log(log, "sid20", "api_server", 800, 600)
_orig_web = mod._trigger_web_turn
web_calls = []
mod._trigger_web_turn = lambda runner, sid, pct: (web_calls.append((sid, round(pct))), True)[1]
cfg_web = {**BASE, "auto_channels": ["telegram", "api_server"]}
ctxw, callsw = run(cfg_web, Store([Entry("agent:main:api_server:dm:sid20", "sid20", "api_server")]), log)
check("web: hand-off turn started once", web_calls == [("sid20", 80)])
check("web: nothing injected into the chat", ctxw.injected == [])
check("web: nothing reset on the server", callsw == [])
state = json.loads(mod._auto_state_path().read_text(encoding="utf-8"))
check("web: state marks the session as web", state.get("sid20", {}).get("stage") == "web")

# 19. the next tick starts no second wake turn and still never resets
web_calls.clear()
ctxw2, callsw2 = run(cfg_web, Store([Entry("agent:main:api_server:dm:sid20", "sid20", "api_server")]), log)
check("web: no second wake turn", web_calls == [])
check("web: still never reset", callsw2 == [])

# 20. a web channel that is not configured is left alone
fresh_state()
write_log(log, "sid21", "api_server", 800, 600)
web_calls.clear()
ctxw3, callsw3 = run(BASE, Store([Entry("agent:main:api_server:dm:sid21", "sid21", "api_server")]), log)
check("web: not configured means no wake turn", web_calls == [] and ctxw3.injected == [])

# 21. the real wake helper refuses cleanly when there is no api_server adapter
mod._trigger_web_turn = _orig_web


class _NoAdapter:
    adapters = {}


check("web wake refuses without an adapter", mod._trigger_web_turn(_NoAdapter(), "sid20", 80) is False)

# 22b. a web session the human left hours ago is a leftover, not a live chat
fresh_state()
write_log(log, "sid23", "api_server", 800, 600)
web_calls.clear()
ctxw5, callsw5 = run(cfg_web, Store([Entry("agent:main:api_server:dm:sid23", "sid23", "api_server")]),
                     log, last_word=lambda sid: time.time() - 8 * 3600)
check("web: a stale human session is left alone", web_calls == [] and ctxw5.injected == [])

# 22. a web mark survives the trigger window: a closed page still finds the block later
fresh_state()
write_log(log, "sid22", "api_server", 800, 600)
mod._save_auto_state({"sid22": {"stage": "web", "ts": time.time() - 3600,
                                "route": "agent:main:api_server:dm:sid22", "platform": "api_server"}})
web_calls.clear()
ctxw4, callsw4 = run(cfg_web, Store([Entry("agent:main:api_server:dm:sid22", "sid22", "api_server")]), log)
check("web: an old mark blocks a second wake turn", web_calls == [] and callsw4 == [])

failed = [n for n, good in ok if not good]
print()
print(f"{len(ok)} checks, {len(failed)} failed")
raise SystemExit(1 if failed else 0)
