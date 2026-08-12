"""Am I running from inside a self-contained Lunchbot.app?

The drag-to-Applications build carries its own CPython at
Contents/Resources/python, so `sys.executable` lives inside the .app and the
bundle root is a fixed walk up from it. A Homebrew or dev-checkout install has
no bundle around it, and every helper here answers None — callers fall back to
the brew/`~/.local/bin` paths they have always used.

(Resources, not Frameworks: codesign enforces framework layout on everything
under Contents/Frameworks and refuses to sign a CPython tree there. See
build-app.sh.)

Stdlib only: the ordering path imports this, and a GUI dependency problem must
never be the reason lunch doesn't happen.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import paths

APP_NAME = "Lunchbot"

# Contents/MacOS/<these>. Three executables, and the split matters:
#
#   APP_EXE  CFBundleExecutable — what Finder/Launchpad/`open` run. It does NOT
#            run the menu bar itself. Started through LaunchServices the process
#            comes up but never paints its status item, so this one asks launchd
#            to start GUI_EXE and then just forwards the "open Preferences"
#            request to that copy.
#   GUI_EXE  the real menu-bar entry point, and the only thing the login
#            LaunchAgent points at. Kept separate from APP_EXE on purpose: if
#            launchd pointed at APP_EXE, APP_EXE's kickstart would restart the
#            very job launchd just started, forever.
#   CLI_EXE  the CLI, which the ordering LaunchAgent runs as `… run`.
#
# None of these may differ from another by case alone. macOS filesystems are
# case-insensitive by default, so Contents/MacOS/lunchbot and
# Contents/MacOS/Lunchbot are one file and whichever the build writes second
# silently wins. That happened: the bundle built, signed and launched, and
# double-clicking it ran the setup wizard in a terminal. test_bundle.py asserts
# the three names stay distinct case-insensitively.
APP_EXE = APP_NAME
GUI_EXE = "lunchbot-gui"
CLI_EXE = "lunchbot-cli"

# Where a dragged app lands, in preference order. /Applications is the drop
# target the DMG advertises; ~/Applications is where the older Homebrew stub
# put itself, and someone may well drag the new one alongside it.
SEARCH_DIRS = (Path("/Applications"), paths.HOME / "Applications")


def _is_bundle(app: Path) -> bool:
    """True for a *self-contained* bundle — one carrying its own interpreter.

    Deliberately stricter than "a directory named Lunchbot.app": the Homebrew
    install also creates a Lunchbot.app, but it is a shell stub that execs the
    brew-installed lunchbot-gui. Treating that as a bundle would point the
    LaunchAgent at a launcher that just re-enters this same resolution.
    """
    return (app / "Contents" / "Resources" / "python" / "bin" / "python3").is_file()


def running_bundle() -> Path | None:
    """The .app this process is executing from, or None outside a bundle."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app" and _is_bundle(parent):
            return parent
    return None


def installed_bundle() -> Path | None:
    """A self-contained Lunchbot.app on disk, whether or not we came from it.

    Prefers the one we're running from, so a copy launched out of ~/Downloads
    schedules itself rather than a stale copy in /Applications.
    """
    running = running_bundle()
    if running is not None:
        return running
    for d in SEARCH_DIRS:
        app = d / f"{APP_NAME}.app"
        if _is_bundle(app):
            return app
    return None


def is_ephemeral(app: Path | None = None) -> bool:
    """True when `app` sits somewhere it will not still be tomorrow.

    Two cases, both routine and both fatal to a LaunchAgent that records an
    absolute path:

      * Run straight from the mounted DMG (/Volumes/...). Ejecting the disk
        image leaves launchd pointing at nothing, so lunch stops happening and
        the menu-bar icon never comes back at login.
      * App Translocation. macOS runs a quarantined app from a randomized
        read-only mount under /private/var/folders/.../AppTranslocation/ rather
        than the path the user double-clicked, and that path is regenerated on
        every launch.

    Either way the answer is the same: copy me to Applications first.
    """
    app = app or running_bundle()
    if app is None:
        return False
    s = str(app.resolve())
    return s.startswith("/Volumes/") or "/AppTranslocation/" in s


def executable(name: str, app: Path | None = None) -> Path | None:
    """Path to Contents/MacOS/<name> in the installed bundle, if there is one."""
    app = app or installed_bundle()
    if app is None:
        return None
    exe = app / "Contents" / "MacOS" / name
    return exe if exe.is_file() else None


def cli() -> Path | None:
    return executable(CLI_EXE)


def gui() -> Path | None:
    return executable(GUI_EXE)


# ---- PATH integration -------------------------------------------------------
# A dragged .app puts nothing on $PATH, but `lunchbot doctor` / `lunchbot status`
# in a terminal is a documented part of using this thing. A symlink into the
# conventional user bin directory is the least invasive fix: no sudo, no shell
# profile edits, and `rm` undoes it.
SHIM_DIR = paths.HOME / ".local" / "bin"
SHIM_PATH = SHIM_DIR / "lunchbot"


def install_cli_shim() -> Path | None:
    """Point ~/.local/bin/lunchbot at the bundle's CLI. Returns the link, or
    None when there's no bundle (Homebrew already owns that name).

    Only ever replaces a symlink. A real file there belongs to install.sh's
    non-Homebrew path, and clobbering someone's working install would be a
    rude way to discover we disagree about who owns the name.
    """
    target = cli()
    if target is None:
        return None
    try:
        SHIM_DIR.mkdir(parents=True, exist_ok=True)
        if SHIM_PATH.is_symlink():
            if SHIM_PATH.resolve() == target.resolve():
                return SHIM_PATH
            SHIM_PATH.unlink()
        elif SHIM_PATH.exists():
            return None
        SHIM_PATH.symlink_to(target)
        return SHIM_PATH
    except OSError:
        return None
