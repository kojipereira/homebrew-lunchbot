"""Preferences entry point. Launched as its own process
(`python -m lunchbot.gui.prefs`) by the menu-bar app, because the prefs window
wants a regular app activation policy and its own NSApplication run loop —
neither of which the menu-bar accessory can hand over.

The window itself lives in [prefs_window.py], imported lazily so that this
module (and the package) stays importable on a plain interpreter with no
pyobjc — the ordering path must never depend on the GUI stack.
"""

from __future__ import annotations

import sys


def main() -> int:
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
