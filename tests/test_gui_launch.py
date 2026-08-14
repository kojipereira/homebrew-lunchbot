"""Launch lifecycle for the menu-bar app: quitting stays quit, and opening
Lunchbot.app lands in the menu bar with Preferences up. Run with the resolved
interpreter:
    PYTHONPATH=src python3.13 tests/test_gui_launch.py
Exits non-zero on failure. No test framework, no real launchctl, no rumps —
everything here is plists, files and locks, so it runs anywhere, Linux included.

Covers the two bugs this file exists for:
  1. `KeepAlive: true` in the GUI LaunchAgent reopened the app seconds after the
     Quit menu item, and an upgrade only kickstarted the job, so a stale plist
     would have kept doing it forever.
  2. Lunchbot.app re-exec'd the menu-bar app with no arguments, so opening it
     added a second bot icon to the menu bar and opened nothing.
"""

import logging
import os
import plistlib
import sys
import tempfile
from datetime import datetime
from pathlib import Path

logging.disable(logging.CRITICAL)   # the swallow paths log expected failures

# Isolate state/config on disk BEFORE importing lunchbot (paths reads env at import).
_tmp = tempfile.mkdtemp(prefix="lunchbot-test-")
os.environ["XDG_STATE_HOME"] = str(Path(_tmp) / "state")
os.environ["XDG_CONFIG_HOME"] = str(Path(_tmp) / "config")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot import agent as A            # noqa: E402
from lunchbot import appbundle as AB       # noqa: E402
from lunchbot import bootstrap as B        # noqa: E402
from lunchbot import singleton             # noqa: E402
from lunchbot.gui import app               # noqa: E402

failures = []


def check(cond, msg):
    print(("ok:   " if cond else "FAIL: ") + msg)
    if not cond:
        failures.append(msg)


class Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))


# --- 1. the GUI LaunchAgent respects a quit ----------------------------------
gui = plistlib.loads(A.generate_gui_plist_bytes())

check(gui["KeepAlive"] == {"SuccessfulExit": False},
      "GUI agent only relaunches after an unclean exit (quit is respected)")
check(gui["KeepAlive"] is not True,
      "GUI agent does not use bare KeepAlive (which reopened the app instantly)")
check(gui["RunAtLoad"] is True, "GUI agent still starts the app at login")
check(gui["ProgramArguments"] == [str(A.GUI_LAUNCHER)],
      "login start does NOT pass --prefs (no window in your face at login)")

# --- 2. a stale plist is rewritten, not kickstarted --------------------------
A.GUI_PLIST_PATH = Path(_tmp) / "com.lunchbot.gui.plist"

check(A.gui_plist_is_current() is False, "a missing GUI plist is not current")

A.GUI_PLIST_PATH.write_bytes(plistlib.dumps({
    "Label": A.GUI_LABEL,
    "ProgramArguments": [str(A.GUI_LAUNCHER)],
    "RunAtLoad": True,
    "KeepAlive": True,                      # the old, unquittable install
}))
check(A.gui_plist_is_current() is False,
      "a legacy KeepAlive:true plist is detected as stale")

A.GUI_PLIST_PATH.write_bytes(A.generate_gui_plist_bytes())
check(A.gui_plist_is_current() is True, "a freshly written GUI plist is current")

# bootstrap must reinstall (rewrite) a stale plist rather than kickstart it,
# even when the job is loaded and the version stamp says there's nothing to do.
AB_install = AB.install_app
AB.install_app = lambda *a, **k: Path(_tmp) / "Lunchbot.app"
B.appbundle.install_app = AB.install_app
A.gui_is_loaded = lambda: True

A.GUI_PLIST_PATH.write_bytes(plistlib.dumps({"KeepAlive": True}))
A.install_gui_agent, A.restart_gui_agent = Spy(), Spy()
B.bootstrap(force=True)
check(len(A.install_gui_agent.calls) == 1 and not A.restart_gui_agent.calls,
      "stale plist -> bootstrap rewrites the LaunchAgent")

A.GUI_PLIST_PATH.write_bytes(A.generate_gui_plist_bytes())
A.install_gui_agent, A.restart_gui_agent = Spy(), Spy()
B.bootstrap(force=True)
check(len(A.restart_gui_agent.calls) == 1 and not A.install_gui_agent.calls,
      "current plist + loaded -> bootstrap only restarts (picks up new code)")

# The stamp is current after those runs, so `auto` would normally skip — a stale
# plist must still get it to run (that's how existing installs are healed).
A.GUI_PLIST_PATH.write_bytes(plistlib.dumps({"KeepAlive": True}))
A.install_gui_agent, A.restart_gui_agent = Spy(), Spy()
B.auto("status")
check(len(A.install_gui_agent.calls) == 1,
      "auto() heals a stale plist even at an already-provisioned version")

A.GUI_PLIST_PATH.write_bytes(A.generate_gui_plist_bytes())
A.install_gui_agent, A.restart_gui_agent = Spy(), Spy()
B.auto("status")
check(not A.install_gui_agent.calls and not A.restart_gui_agent.calls,
      "auto() stays a no-op once the stamp and the plist are both current")

B.auto("gui")   # the menu-bar app itself must never churn launchd on the way in
check(not A.install_gui_agent.calls, "auto('gui') never touches the LaunchAgent")

AB.install_app = AB_install
B.appbundle.install_app = AB_install

# --- 3. the instance lock ----------------------------------------------------
check(singleton.is_held("selftest") is False, "an unheld lock reads as free")

first = singleton.InstanceLock("selftest")
check(first.acquire() is True, "the first process takes the lock")
check(singleton.is_held("selftest") is True, "a held lock reads as held")
check(first.owner_pid() == os.getpid(), "the lock file records the owner's pid")

second = singleton.InstanceLock("selftest")
check(second.acquire() is False, "a second process is refused the lock")
check(second.owner_pid() == os.getpid(),
      "the loser can still read the winner's pid (to raise its window)")

first.release()
check(second.acquire() is True, "releasing hands the lock to the next process")
second.release()

# --- 4. opening Lunchbot.app -------------------------------------------------
# Every reachable exec passes --prefs: once via GUI_BIN (covers both the brew
# and dev-install launcher paths, resolved once), once via the `lunchbot gui`
# fallback.
check(AB._LAUNCHER_SCRIPT.count("--prefs") == 2,
      "Lunchbot.app passes --prefs down every launcher fallback")
# An unsigned bundle launched via Finder/`open` can start the process but
# never paint anything in the menu bar on some macOS versions (proven by
# testing) — kickstarting the already-registered login agent first, before
# falling through to direct exec, is the actual fix; both must be present.
check("launchctl kickstart" in AB._LAUNCHER_SCRIPT,
      "opening Lunchbot.app kickstarts the login agent if it's registered")
check(A.GUI_LABEL in AB._LAUNCHER_SCRIPT,
      "the launcher targets the same LaunchAgent label agent.py registers")
check(AB._LAUNCHER_SCRIPT.index("launchctl kickstart")
      < AB._LAUNCHER_SCRIPT.rindex("--prefs"),
      "the kickstart attempt happens before the direct-exec fallback")

# --- an "order tomorrow" override is surfaced, not an invisible state -------
from lunchbot.config import Config, Favorite, write_config  # noqa: E402
from lunchbot.state import set_override  # noqa: E402

write_config(Config(
    lunch_time="12:00", weekdays=[1, 2, 3, 4, 5],
    favorites=[Favorite("Overridden Spot", "1", "1", lead_minutes=30)],
))


class _FixedNow(datetime):
    @classmethod
    def now(cls):
        return cls(2026, 8, 11, 9, 0)   # a Tuesday, before the 11:30 fire


old_dt, old_loaded = app.datetime, A.is_loaded
app.datetime = _FixedNow
A.is_loaded = lambda: True
try:
    check(app._status_text() == "Next: Tue 11:30",
          "no override -> plain 'Next:' status")
    set_override("2026-08-11", "Overridden Spot")
    check(app._status_text() == "Next: Tue 11:30 → Overridden Spot",
          "an override for the next fire date is surfaced in the status line")
finally:
    app.datetime = old_dt
    A.is_loaded = old_loaded

check(app._parse_args([]).prefs is False, "plain `lunchbot-gui` opens no window")
check(app._parse_args(["--prefs"]).prefs is True, "--prefs is understood")

# A second copy (icon already in the menu bar) must not start a second app: it
# hands the request over and exits 0 — non-zero would make launchd relaunch it.
held = singleton.InstanceLock("gui")
check(held.acquire() is True, "test holds the gui lock (standing in for the app)")
app._spawn_prefs = Spy()
rc = app.main(["--prefs"])
check(rc == 0 and len(app._spawn_prefs.calls) == 1,
      "already running + --prefs -> opens Preferences, exits 0, no second icon")

app._spawn_prefs = Spy()
rc = app.main([])
check(rc == 0 and not app._spawn_prefs.calls,
      "already running, no --prefs -> exits 0 quietly")
held.release()

# --- 5. `lunchbot gui --prefs` routes the flag through -----------------------
from lunchbot import __main__ as M           # noqa: E402

app.main = Spy()
M.main(["gui", "--prefs"])
check(app.main.calls and app.main.calls[0][0] == (["--prefs"],),
      "`lunchbot gui --prefs` forwards the flag to the menu-bar app")

app.main = Spy()
M.main(["gui"])
check(app.main.calls and app.main.calls[0][0] == ([],),
      "`lunchbot gui` forwards no flag")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all tests passed")
