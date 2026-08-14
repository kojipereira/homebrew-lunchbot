"""Generate a minimal, double-clickable Lunchbot.app wrapper.

Not a py2app/notarized bundle — just the smallest valid .app: an Info.plist
plus a shell launcher that execs the menu-bar app. Double-clickable from Finder,
draggable to the Dock/Desktop. Unsigned, so the first launch needs a one-time
right-click → Open (Gatekeeper). Stdlib-only; imports no GUI deps.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from . import paths
from .__init__ import __version__

APP_NAME = "Lunchbot"
BUNDLE_ID = "com.lunchbot.app"
DEFAULT_APP_DIR = paths.HOME / "Applications"

# Built from assets/lunchbot.icon by tools/build-icon.py and committed, so an
# install never needs a rasterizer. Missing is survivable: the .app still works,
# it just falls back to the generic Unix-executable icon.
ICON_SOURCE = Path(__file__).resolve().parent / "resources" / f"{APP_NAME}.icns"
ICON_FILE = f"{APP_NAME}.icns"

# Resolve the GUI launcher at click-time (brew symlink first, then dev install),
# falling back to `lunchbot gui`, and finally a helpful alert if nothing's found.
#
# --prefs throughout: opening the app from Finder, Launchpad or the Dock means
# "show me Lunchbot", so it puts the bot icon in the menu bar and opens
# Preferences. If the menu-bar app is already running, that copy keeps the icon
# and this one only opens the window (see gui/app.py's instance lock).
#
# Launched this way (Finder double-click / `open`), an unsigned bundle can
# start the process fine but never actually paint anything in the menu bar on
# some macOS versions — proven by testing the identical code both ways. The
# login LaunchAgent (com.lunchbot.gui, registered by `lunchbot bootstrap`)
# starts the same process via launchd directly and always renders correctly,
# so kickstart *that* first when it's already registered, then still launch
# --prefs: if the kickstarted copy won the singleton lock (see gui/app.py),
# this invocation just hands off to it and opens the window, exactly as if
# it had been the one running the icon all along.
_LAUNCHER_SCRIPT = """\
#!/bin/sh
GUI_BIN=""
for c in /opt/homebrew/bin/lunchbot-gui "$HOME/.local/bin/lunchbot-gui"; do
  [ -x "$c" ] && GUI_BIN="$c" && break
done
if [ -z "$GUI_BIN" ] && command -v lunchbot-gui >/dev/null 2>&1; then
  GUI_BIN="lunchbot-gui"
fi

if [ -n "$GUI_BIN" ]; then
  AGENT="gui/$(id -u)/com.lunchbot.gui"
  if launchctl print "$AGENT" >/dev/null 2>&1; then
    launchctl kickstart -k "$AGENT" >/dev/null 2>&1
    sleep 1
  fi
  exec "$GUI_BIN" --prefs
fi

if command -v lunchbot >/dev/null 2>&1; then exec lunchbot gui --prefs; fi
exec /usr/bin/osascript -e 'display alert "Lunchbot" message "lunchbot is not on your PATH. Install it (brew install ... / ./install.sh) and try again."'
"""


def _info_plist(icon: bool = True) -> bytes:
    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "CFBundlePackageType": "APPL",
        # Menu-bar accessory: no Dock icon while running, still double-clickable.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "11.0",
    }
    if icon:
        # CFBundleIconFile names Contents/Resources/Lunchbot.icns.
        info["CFBundleIconFile"] = ICON_FILE
    return plistlib.dumps(info)


def install_app(app_dir=None) -> "paths.Path":
    """Create <app_dir>/Lunchbot.app and return its path. Overwrites any prior
    copy so it's safe to re-run after an upgrade.

    No-op when we're already running from a self-contained bundle (the
    drag-to-Applications build): that copy *is* the app, it carries its own
    interpreter, and writing a stub next to it would leave the user with two
    Lunchbot.apps — the second of which only knows how to look for a Homebrew
    install that may not exist. Instead we make sure `lunchbot` is reachable
    from a terminal, which a dragged .app otherwise isn't.
    """
    import shutil

    from . import bundle

    running = bundle.running_bundle()
    if running is not None and app_dir is None:
        bundle.install_cli_shim()
        return running

    base = (app_dir or DEFAULT_APP_DIR)

    # Never overwrite a self-contained bundle with the stub. Someone who is not
    # an admin cannot drag to /Applications without a password, so ~/Applications
    # is a normal place for the real app to land — and that is exactly where this
    # function writes. Left unguarded, the next CLI command from a Homebrew
    # install alongside it would replace 60 MB of working app with a shell
    # script, and the user would never know why Lunchbot stopped opening.
    existing = base / f"{APP_NAME}.app"
    if bundle._is_bundle(existing):
        bundle.install_cli_shim()
        return existing
    app = base / f"{APP_NAME}.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)

    has_icon = ICON_SOURCE.is_file()
    if has_icon:
        resources = app / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ICON_SOURCE, resources / ICON_FILE)

    (app / "Contents" / "Info.plist").write_bytes(_info_plist(icon=has_icon))
    launcher = macos / APP_NAME
    launcher.write_text(_LAUNCHER_SCRIPT)
    launcher.chmod(0o755)

    # Finder caches icons per bundle mtime; without this an upgrade can keep
    # showing the old (or generic) icon until the cache happens to turn over.
    os.utime(app, None)
    return app


def uninstall_app(app_dir=None) -> bool:
    """Remove Lunchbot.app. Returns True if it existed."""
    import shutil

    from . import bundle

    # Drop the terminal shim first — a symlink into a bundle we're about to
    # delete is worse than no shim at all.
    try:
        if bundle.SHIM_PATH.is_symlink():
            bundle.SHIM_PATH.unlink()
    except OSError:
        pass

    app = (app_dir or DEFAULT_APP_DIR) / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
        return True
    return False
