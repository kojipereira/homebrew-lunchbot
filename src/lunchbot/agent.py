"""launchd LaunchAgent management.

The plist points at the stable launcher (~/.local/bin/lunchbot run), never at a
python path — so `brew upgrade python` never requires rewriting it. Fire times
are generated from the config's distinct lead tiers. Uses the modern
bootstrap/bootout/enable/kickstart verbs in the gui/<uid> domain.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
from pathlib import Path

from . import bundle, paths
from .config import Config

LABEL = "com.lunchbot.agent"          # daily ordering agent
GUI_LABEL = "com.lunchbot.gui"        # menu-bar app (see gui_agent helpers below)

# A self-contained Lunchbot.app wins when there is one: its executables are the
# only ones that keep working with no Homebrew on the machine, and the path is
# stable because the bundle is the unit that gets replaced on upgrade. Failing
# that, the stable Homebrew symlink (survives `brew upgrade`), then the
# hand-installed launcher for the non-Homebrew dev path.
#
# `name` and `bundle_exe` differ because the bundle's GUI executable has to be
# CFBundleExecutable ("Lunchbot"), while Homebrew installs a console script
# called "lunchbot-gui".
def _resolve_launcher(name: str, bundle_exe: str) -> Path:
    exe = bundle.executable(bundle_exe)
    if exe is not None:
        return exe
    brew = Path("/opt/homebrew/bin") / name
    return brew if brew.exists() else paths.HOME / ".local" / "bin" / name


LAUNCHER = _resolve_launcher("lunchbot", bundle.CLI_EXE)
GUI_LAUNCHER = _resolve_launcher("lunchbot-gui", bundle.GUI_EXE)
PLIST_PATH = paths.HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
GUI_PLIST_PATH = paths.HOME / "Library" / "LaunchAgents" / f"{GUI_LABEL}.plist"
STDOUT_LOG = paths.HOME / "Library" / "Logs" / "lunchbot.stdout.log"
STDERR_LOG = paths.HOME / "Library" / "Logs" / "lunchbot.stderr.log"
GUI_STDOUT_LOG = paths.HOME / "Library" / "Logs" / "lunchbot.gui.log"

# Legacy labels from earlier installs (migration targets — torn down on setup).
LEGACY_LABELS = ("com.koji.lunchbot",)

_AGENT_PATH = "/opt/homebrew/bin:" + str(paths.HOME / ".local" / "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def fire_times(cfg: Config) -> list[tuple[int, int]]:
    """(hour, minute) tuples: for each distinct lead tier among the favorites,
    fire at lunch_time − lead_minutes."""
    lh, lm = (int(x) for x in cfg.lunch_time.split(":"))
    lunch_min = lh * 60 + lm
    leads = sorted({f.lead_minutes for f in cfg.favorites})
    times = set()
    for lead in leads:
        t = (lunch_min - lead) % (24 * 60)
        times.add((t // 60, t % 60))
    return sorted(times)


def generate_plist_bytes(cfg: Config) -> bytes:
    times = fire_times(cfg)
    intervals = [{"Weekday": wd, "Hour": h, "Minute": m}
                 for wd in cfg.weekdays for (h, m) in times]
    env = {"PATH": _AGENT_PATH}
    # Pass a corporate CA bundle through so the agent inherits it (proxied nets).
    ca = os.environ.get("DD_CLI_CA_BUNDLE")
    if ca:
        env["DD_CLI_CA_BUNDLE"] = ca
    d = {
        "Label": LABEL,
        "ProgramArguments": [str(LAUNCHER), "run"],
        "StartCalendarInterval": intervals,
        "EnvironmentVariables": env,
        "StandardOutPath": str(STDOUT_LOG),
        "StandardErrorPath": str(STDERR_LOG),
        "RunAtLoad": False,
        "LimitLoadToSessionType": "Aqua",
    }
    return plistlib.dumps(d)


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    cmd = ["launchctl", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    logging.info("launchctl %s → rc=%d %s", " ".join(args), r.returncode,
                 (r.stderr or "").strip())
    if check and r.returncode != 0:
        raise RuntimeError(f"launchctl {' '.join(args)} failed: {(r.stderr or '').strip()}")
    return r


def install_agent(cfg: Config) -> None:
    if not fire_times(cfg):
        raise RuntimeError(
            "No favorites, so no fire times to schedule. "
            "Add favorites with `lunchbot setup`."
        )
    if not LAUNCHER.exists():
        raise RuntimeError(f"launcher not found at {LAUNCHER}; run install.sh first")
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_bytes(generate_plist_bytes(cfg))
    dom = _domain()
    _launchctl("bootout", f"{dom}/{LABEL}")            # ignore "not loaded"
    # `enable` MUST come before `bootstrap`. launchd refuses to bootstrap a
    # disabled job — it fails with the gloriously unhelpful "Bootstrap failed:
    # 5: Input/output error" — so with the old ordering, pause_agent() (which
    # disables) left the schedule permanently unresumable: bootstrap raised
    # before the enable that was meant to undo the disable ever ran. The user
    # saw "Couldn't resume", or, from _restore_schedule_quietly, nothing at all.
    _launchctl("enable", f"{dom}/{LABEL}")
    _launchctl("bootstrap", dom, str(PLIST_PATH), check=True)
    logging.info("agent installed: %d fire times × %d weekdays",
                 len(fire_times(cfg)), len(cfg.weekdays))


def uninstall_agent() -> None:
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()


def stop_agent() -> None:
    """Unload the schedule without disabling it — used when the menu-bar app
    closes, so nothing fires while it's shut. The plist stays on disk and is
    reinstalled on next launch. Distinct from pause_agent(), which also
    `disable`s to persist a user-initiated pause across relaunches."""
    _launchctl("bootout", f"{_domain()}/{LABEL}")


def pause_agent() -> None:
    dom = _domain()
    _launchctl("bootout", f"{dom}/{LABEL}")
    _launchctl("disable", f"{dom}/{LABEL}")


def resume_agent(cfg: Config) -> None:
    install_agent(cfg)


def test_fire() -> None:
    _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}", check=True)


def migrate_legacy() -> None:
    """Tear down any earlier-labeled agents from pre-1.1 installs."""
    for label in LEGACY_LABELS:
        legacy_plist = paths.HOME / "Library" / "LaunchAgents" / f"{label}.plist"
        _launchctl("bootout", f"{_domain()}/{label}")
        _launchctl("unload", str(legacy_plist))  # legacy may have been `load`ed
        if legacy_plist.exists():
            legacy_plist.unlink()


def is_loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


# ---- GUI menu-bar agent -----------------------------------------------------
# A LaunchAgent that starts the rumps menu-bar app at login and brings it back if
# it dies unexpectedly. Kept distinct from the ordering agent so a GUI failure
# can never affect the scheduled `lunchbot run`.
def generate_gui_plist_bytes() -> bytes:
    env = {"PATH": _AGENT_PATH}
    ca = os.environ.get("DD_CLI_CA_BUNDLE")
    if ca:
        env["DD_CLI_CA_BUNDLE"] = ca
    d = {
        "Label": GUI_LABEL,
        # No --prefs here: starting at login means "put the bot icon in the menu
        # bar", never "open a window in someone's face".
        "ProgramArguments": [str(GUI_LAUNCHER)],
        "EnvironmentVariables": env,
        "StandardOutPath": str(GUI_STDOUT_LOG),
        "StandardErrorPath": str(GUI_STDOUT_LOG),
        "RunAtLoad": True,
        # Quit means quit. Plain `KeepAlive: true` relaunched the app within
        # seconds of the Quit menu item, so the icon could not be dismissed at
        # all; keying on SuccessfulExit respects a clean exit and still restarts
        # the app if it crashes or is killed. It comes back at the next login
        # (RunAtLoad), which is what `lunchbot uninstall-gui-agent` turns off.
        "KeepAlive": {"SuccessfulExit": False},
        "LimitLoadToSessionType": "Aqua",
    }
    return plistlib.dumps(d)


def install_gui_agent() -> None:
    if not GUI_LAUNCHER.exists():
        raise RuntimeError(f"GUI launcher not found at {GUI_LAUNCHER}; "
                           "drag Lunchbot.app to Applications, install via "
                           "Homebrew, or run install.sh")
    GUI_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUI_PLIST_PATH.write_bytes(generate_gui_plist_bytes())
    dom = _domain()
    _launchctl("bootout", f"{dom}/{GUI_LABEL}")
    _launchctl("enable", f"{dom}/{GUI_LABEL}")   # before bootstrap — see install_agent
    _launchctl("bootstrap", dom, str(GUI_PLIST_PATH), check=True)


def uninstall_gui_agent() -> None:
    _launchctl("bootout", f"{_domain()}/{GUI_LABEL}")
    if GUI_PLIST_PATH.exists():
        GUI_PLIST_PATH.unlink()


def restart_gui_agent() -> None:
    """Best-effort relaunch (used after `brew upgrade` to pick up new code)."""
    _launchctl("kickstart", "-k", f"{_domain()}/{GUI_LABEL}")


def gui_is_loaded() -> bool:
    """True when launchd knows the job — i.e. the menu-bar app starts at login.
    Stays true after a deliberate Quit: the job is registered, just not running.
    Use singleton.is_held("gui") to ask whether the app is up right now."""
    return _launchctl("print", f"{_domain()}/{GUI_LABEL}").returncode == 0


def gui_plist_is_current() -> bool:
    """True when the installed GUI plist matches what this version generates.

    Installs from before the KeepAlive fix carry `KeepAlive: true`, which
    relaunches the app the moment you quit it. Kickstarting such a job would
    keep the old behaviour forever, so bootstrap rewrites the file instead —
    this is how it tells the difference. Cheap: one small read, no launchctl."""
    try:
        return GUI_PLIST_PATH.read_bytes() == generate_gui_plist_bytes()
    except OSError:
        return False
