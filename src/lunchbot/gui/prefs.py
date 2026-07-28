"""Preferences entry point. Launched as its own process
(`python -m lunchbot.gui.prefs`) by the menu-bar app, because the prefs window
wants a regular app activation policy and its own NSApplication run loop —
neither of which the menu-bar accessory can hand over.

Because it's a process per request, opening Preferences twice would open two
windows, so this holds a single-instance lock (see [singleton.py]) and the second
caller just brings the existing window to the front.

The window itself lives in [prefs_window.py], imported lazily so that this
module (and the package) stays importable on a plain interpreter with no
pyobjc — the ordering path must never depend on the GUI stack.
"""

from __future__ import annotations

import logging
import sys

from .. import singleton

# Spelled out rather than imported: pyobjc deprecated the constant, and this is
# the one value we need from it.
ACTIVATE_IGNORING_OTHER_APPS = 1 << 1   # NSApplicationActivateIgnoringOtherApps

# Module-level so the lock lives as long as the process; see singleton.py.
_instance_lock = None


def _activate(pid) -> None:
    """Raise the Preferences window that's already open, in another process."""
    if not pid:
        return
    try:
        from AppKit import NSRunningApplication
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if running is not None:
            running.activateWithOptions_(ACTIVATE_IGNORING_OTHER_APPS)
    except Exception:  # noqa: BLE001 — the window is open either way
        logging.info("could not foreground the open Preferences window (pid %s)", pid)


def main() -> int:
    global _instance_lock
    _instance_lock = singleton.InstanceLock("prefs")
    if not _instance_lock.acquire():
        _activate(_instance_lock.owner_pid())
        return 0

    try:
        from . import prefs_window
    except ImportError as e:
        print("The preferences window needs pyobjc (installed in the Homebrew "
              f"venv alongside rumps): {e}\n"
              "Meanwhile, `lunchbot setup` does the same job in the terminal.",
              file=sys.stderr)
        return 1
    return prefs_window.run()


if __name__ == "__main__":
    sys.exit(main())
