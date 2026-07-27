"""Schedule lifecycle: the menu-bar app stops the schedule on close and
restores its last state on launch. Run with the resolved interpreter:
    PYTHONPATH=src python3.13 tests/test_lifecycle.py
Exits non-zero on failure. No test framework, and no real `launchctl`:
the agent calls are mocked so this is safe to run on a live machine.
"""

import logging
import os
import sys
import tempfile
from pathlib import Path

logging.disable(logging.CRITICAL)  # the swallow paths log expected tracebacks

# Isolate state/config on disk BEFORE importing lunchbot (paths reads env at import).
_tmp = tempfile.mkdtemp(prefix="lunchbot-test-")
os.environ["XDG_STATE_HOME"] = str(Path(_tmp) / "state")
os.environ["XDG_CONFIG_HOME"] = str(Path(_tmp) / "config")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot import agent as A            # noqa: E402
from lunchbot import state as S            # noqa: E402
from lunchbot.config import ConfigError    # noqa: E402
from lunchbot.gui import app               # noqa: E402

REAL_STOP_AGENT = A.stop_agent             # keep a handle before monkeypatching

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")
    else:
        print(f"ok:   {msg}")


class Spy:
    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        if self.raises:
            raise self.raises


# --- state intent round-trips ------------------------------------------------
check(S.load_state()["schedule_paused"] is False, "schedule_paused defaults to False")
S.set_schedule_paused(True)
check(S.load_state()["schedule_paused"] is True, "set_schedule_paused(True) persists")
S.set_schedule_paused(False)
check(S.load_state()["schedule_paused"] is False, "set_schedule_paused(False) persists")

# --- stop on close: delegates to agent.stop_agent, never raises --------------
A.stop_agent = Spy()
app._stop_schedule_quietly()
check(len(A.stop_agent.calls) == 1, "close stops the schedule via agent.stop_agent")

A.stop_agent = Spy(raises=RuntimeError("launchctl boom"))
try:
    app._stop_schedule_quietly()
    check(True, "close swallows agent errors (never blocks shutdown)")
except Exception:  # noqa: BLE001
    check(False, "close swallows agent errors (never blocks shutdown)")

# --- restore on launch --------------------------------------------------------
FAKE_CFG = object()

# (a) not set up yet -> nothing restored
A.resume_agent = Spy()
app.load_config = lambda: (_ for _ in ()).throw(ConfigError("no config"))
app._restore_schedule_quietly()
check(len(A.resume_agent.calls) == 0, "no config -> schedule not restored")

# (b) config present, last state active -> schedule restored with that config
A.resume_agent = Spy()
app.load_config = lambda: FAKE_CFG
S.set_schedule_paused(False)
app._restore_schedule_quietly()
check([c[0] for c in A.resume_agent.calls] == [(FAKE_CFG,)],
      "active last state -> schedule restored from current config")

# (c) config present, last state paused -> schedule stays paused
A.resume_agent = Spy()
app.load_config = lambda: FAKE_CFG
S.set_schedule_paused(True)
app._restore_schedule_quietly()
check(len(A.resume_agent.calls) == 0, "paused last state -> schedule NOT auto-restored")

# (d) restore never propagates agent errors
A.resume_agent = Spy(raises=RuntimeError("install boom"))
app.load_config = lambda: FAKE_CFG
S.set_schedule_paused(False)
try:
    app._restore_schedule_quietly()
    check(True, "restore swallows agent errors (never blocks startup)")
except Exception:  # noqa: BLE001
    check(False, "restore swallows agent errors (never blocks startup)")

# --- stop_agent must NOT disable (that's reserved for a user pause) ----------
calls = []
A._launchctl = lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})()
REAL_STOP_AGENT()
check(calls == [("bootout", f"gui/{os.getuid()}/{A.LABEL}")],
      "stop_agent only boots out (no disable), unlike pause_agent")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all tests passed")
