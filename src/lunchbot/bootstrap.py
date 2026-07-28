"""First-run wiring: get Lunchbot into ~/Applications and the menu bar without
asking anyone to run `install-app` / `install-gui-agent` by hand.

Homebrew can't do this at install time. `post_install` runs inside a sandbox
that explicitly calls `deny_read_home` (see Homebrew's formula_installer.rb), so
a formula is structurally incapable of creating `~/Applications/Lunchbot.app` or
a LaunchAgent under `~/Library`. Rather than paper over that with instructions
in `caveats`, lunchbot provisions itself the first time any of its commands runs
after an install or an upgrade.

A stamp file records the version we last provisioned for, so this costs one
`launchctl print` on the first command after an upgrade and nothing after that.
Every step is best-effort: a machine where this can't work still gets a fully
functional CLI, and the stamp isn't written, so the next command tries again.
"""

from __future__ import annotations

import json
import logging

from . import agent, appbundle, paths
from .__init__ import __version__

STAMP_PATH = paths.STATE_DIR / "bootstrap.json"

# Commands that must not provision on the way in:
#   run    — the scheduled order; never churn launchd mid-lunch
#   gui    — it *is* the menu-bar app; restarting the agent would kill it
#   prefs  — a child of the menu-bar app, same reason
#   bootstrap / uninstall-* — they manage this state explicitly
SKIP_COMMANDS = frozenset({
    "run", "gui", "prefs", "bootstrap",
    "uninstall-agent", "uninstall-gui-agent", "uninstall-app",
})


def _stamp_is_current() -> bool:
    try:
        return json.loads(STAMP_PATH.read_text()).get("version") == __version__
    except (OSError, ValueError, AttributeError):
        return False


def _write_stamp() -> None:
    try:
        STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STAMP_PATH.write_text(json.dumps({"version": __version__}) + "\n")
    except OSError as e:
        logging.info("could not write bootstrap stamp: %s", e)


def bootstrap(force: bool = False, gui_agent: bool = True) -> list[str]:
    """Create Lunchbot.app and register the menu-bar LaunchAgent.

    Idempotent. Returns a list of human-readable actions taken (empty when
    there was nothing to do). Never raises: callers are install paths where a
    failure here must not break anything else.
    """
    if not force and _stamp_is_current():
        return []

    actions: list[str] = []
    complete = True

    try:
        app = appbundle.install_app()
        actions.append(f"created {app}")
    except OSError as e:
        complete = False
        logging.info("bootstrap: could not create Lunchbot.app: %s", e)

    if gui_agent:
        try:
            if agent.gui_is_loaded():
                # Already running — an upgrade needs it to pick up the new code.
                agent.restart_gui_agent()
                actions.append("restarted the menu-bar app")
            else:
                agent.install_gui_agent()
                actions.append("menu-bar app registered (starts at login)")
        except Exception as e:  # noqa: BLE001 — launchctl/RuntimeError, all non-fatal
            complete = False
            logging.info("bootstrap: could not register the menu-bar app: %s", e)

    if complete:
        _write_stamp()
    return actions


def auto(cmd: str) -> None:
    """Provision on the way into a CLI command, silently. Called for every
    command not in SKIP_COMMANDS."""
    if cmd in SKIP_COMMANDS or _stamp_is_current():
        return
    try:
        bootstrap()
    except Exception:  # noqa: BLE001 — belt and braces; this must never block a command
        logging.exception("auto-bootstrap failed (ignored)")
