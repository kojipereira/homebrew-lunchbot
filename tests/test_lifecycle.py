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
from types import SimpleNamespace

logging.disable(logging.CRITICAL)  # the swallow paths log expected tracebacks

# Isolate state/config on disk BEFORE importing lunchbot (paths reads env at import).
_tmp = tempfile.mkdtemp(prefix="lunchbot-test-")
os.environ["XDG_STATE_HOME"] = str(Path(_tmp) / "state")
os.environ["XDG_CONFIG_HOME"] = str(Path(_tmp) / "config")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot import __main__ as M         # noqa: E402
from lunchbot import agent as A            # noqa: E402
from lunchbot import bootstrap             # noqa: E402
from lunchbot import paths                 # noqa: E402
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

# --- `lunchbot pause` / `lunchbot resume` record the same intent as the GUI ---
# Without this the CLI and the menu-bar app disagree: a CLI pause would be
# undone by the app's next launch-time restore, and a CLI resume would be
# re-paused the next time the app quits.
paths.setup_logging = lambda: None       # keep the real ~/Library log untouched
bootstrap.auto = lambda cmd: None        # no provisioning side effects in tests
M.load_config = lambda: FAKE_CFG         # `resume` config-loads before installing

A.pause_agent = Spy()
S.set_schedule_paused(False)
check(M.main(["pause"]) == 0, "`lunchbot pause` exits 0")
check(len(A.pause_agent.calls) == 1, "`lunchbot pause` disables the launchd job")
check(S.load_state()["schedule_paused"] is True,
      "`lunchbot pause` persists schedule_paused=True (survives an app relaunch)")

A.resume_agent = Spy()
S.set_schedule_paused(True)   # as any prior pause, CLI or GUI, would leave it
check(M.main(["resume"]) == 0, "`lunchbot resume` exits 0")
check([c[0] for c in A.resume_agent.calls] == [(FAKE_CFG,)],
      "`lunchbot resume` reinstalls the job from the current config")
check(S.load_state()["schedule_paused"] is False,
      "`lunchbot resume` persists schedule_paused=False (app stops re-pausing)")

# CLI pause from an active schedule, then app relaunch: the pause has to survive.
A.pause_agent = Spy()
A.resume_agent = Spy()
S.set_schedule_paused(False)
M.main(["pause"])
app._restore_schedule_quietly()
check(len(A.resume_agent.calls) == 0,
      "CLI pause survives a menu-bar app relaunch (no silent un-pause)")

# CLI resume from a paused schedule, then app relaunch: restore has to happen.
A.resume_agent = Spy()
S.set_schedule_paused(True)
M.main(["resume"])
app._restore_schedule_quietly()
check([c[0] for c in A.resume_agent.calls] == [(FAKE_CFG,), (FAKE_CFG,)],
      "CLI resume is honoured by the next menu-bar app launch")

# --- enable must precede bootstrap ------------------------------------------
# launchd refuses to bootstrap a *disabled* job, failing with "Bootstrap
# failed: 5: Input/output error". With enable after bootstrap, pause_agent()
# (which disables) left the schedule permanently unresumable — bootstrap raised
# before the enable meant to undo it ever ran. Nothing about this ordering is
# visible until someone pauses, so pin it here.
#
# Unlike everything above, this drives the real install_agent/install_gui_agent,
# which WRITE A PLIST. Their paths hang off paths.HOME, which the XDG variables
# at the top of this file do not redirect — so without pointing them at the temp
# dir, running this suite would overwrite the live ~/Library/LaunchAgents plists
# of whoever ran it. Redirected last, and left that way, so nothing after can
# reach the real ones either.
A.PLIST_PATH = Path(_tmp) / "com.lunchbot.agent.plist"
A.GUI_PLIST_PATH = Path(_tmp) / "com.lunchbot.gui.plist"

# install_agent derives fire times from the config, so it needs more than
# FAKE_CFG's bare object().
SCHED_CFG = SimpleNamespace(
    lunch_time="12:00",
    weekdays=[1, 2, 3, 4, 5],
    favorites=[SimpleNamespace(lead_minutes=30)],
)

for label, install in (
    (A.LABEL, lambda: A.install_agent(SCHED_CFG)),
    (A.GUI_LABEL, A.install_gui_agent),
):
    calls = []
    A._launchctl = lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})()
    saved_exists = Path.exists
    try:
        Path.exists = lambda self: True          # launcher presence check
        install()
    finally:
        Path.exists = saved_exists
    verbs = [c[0] for c in calls]
    check("enable" in verbs and "bootstrap" in verbs
          and verbs.index("enable") < verbs.index("bootstrap"),
          f"{label}: enable precedes bootstrap (a disabled job can't bootstrap)")

check(A.PLIST_PATH.is_file() and A.GUI_PLIST_PATH.is_file()
      and str(A.PLIST_PATH).startswith(_tmp),
      "the plists this test wrote landed in the temp dir, not ~/Library/LaunchAgents")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all tests passed")
