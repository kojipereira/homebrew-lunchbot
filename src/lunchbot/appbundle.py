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
_LAUNCHER_SCRIPT = """\
#!/bin/sh
for c in /opt/homebrew/bin/lunchbot-gui "$HOME/.local/bin/lunchbot-gui"; do
  [ -x "$c" ] && exec "$c"
done
if command -v lunchbot-gui >/dev/null 2>&1; then exec lunchbot-gui; fi
if command -v lunchbot >/dev/null 2>&1; then exec lunchbot gui; fi
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
    copy so it's safe to re-run after an upgrade."""
    import shutil

    base = (app_dir or DEFAULT_APP_DIR)
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
    app = (app_dir or DEFAULT_APP_DIR) / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
        return True
    return False
