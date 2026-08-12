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

from . import agent, appbundle, bundle, paths
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
    # Running from the DMG or an App Translocation mount: every path we would
    # write into a plist disappears on eject or on the next launch, so register
    # nothing and say what to do instead.
    if bundle.is_ephemeral():
        return ["Lunchbot is running from a disk image. Drag it to your "
                "Applications folder and open it from there."]

    # A current stamp isn't quite enough: a machine can sit at this version with
    # an older LaunchAgent (one that reopens the app the moment it's quit), so
    # the plist is checked too whenever we're responsible for it.
    if not force and _stamp_is_current() and (not gui_agent
                                              or agent.gui_plist_is_current()):
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
            if agent.gui_plist_is_current() and agent.gui_is_loaded():
                # Registered and up to date — but an upgrade still needs the
                # running copy to pick up the new code.
                agent.restart_gui_agent()
                actions.append("restarted the menu-bar app")
            else:
                # Missing, not loaded, or stale. Stale is the one that matters:
                # older plists relaunch the app the instant you quit it, and a
                # kickstart would preserve that forever. Rewriting re-bootstraps
                # the job, which restarts the app too.
                agent.install_gui_agent()
                actions.append("menu-bar app registered (starts at login)")
        except Exception as e:  # noqa: BLE001 — launchctl/RuntimeError, all non-fatal
            complete = False
            logging.info("bootstrap: could not register the menu-bar app: %s", e)

    if complete:
        _write_stamp()
    return actions


def provision_from_bundle() -> list[str]:
    """Self-provision when the menu-bar app is a dragged-in Lunchbot.app.

    Homebrew installs get their wiring from `lunchbot bootstrap` in the
    installer script and from auto() on the way into any CLI command. A
    dragged .app gets neither: double-clicking it enters gui.app.main()
    directly, so without this nothing ever registers the login agent and the
    menu-bar icon would not come back after a reboot.

    Only touches the login agent — the ordering schedule is restored separately
    from the user's config, and re-registering it here would fire launchctl
    twice on every launch for no reason.

    Best-effort and quiet: this runs on a background thread while the menu is
    already up, and a failure must never take the app down with it.
    """
    if bundle.running_bundle() is None:
        return []                      # Homebrew/dev install — auto() covers it
    if bundle.is_ephemeral():
        return ["running from a disk image; drag Lunchbot to Applications"]

    actions: list[str] = []
    if bundle.install_cli_shim() is not None:
        actions.append(f"linked {bundle.SHIM_PATH}")

    # Only when the plist is missing or stale. Rewriting it means `bootout`,
    # which would kill this very process if launchd is what started us — and
    # when the plist is already current there is nothing to gain by doing so.
    #
    # On a genuine first run we are a Finder double-click, not a launchd job,
    # so the bootout below hits a job that isn't running. `bootstrap` then
    # starts the job, that copy loses the singleton race, exits 0, and
    # KeepAlive/SuccessfulExit leaves it registered but not running. We keep
    # the menu bar; launchd takes over at the next login.
    try:
        if not agent.gui_plist_is_current():
            agent.install_gui_agent()
            actions.append("menu-bar app registered (starts at login)")
    except Exception as e:  # noqa: BLE001 — launchctl/RuntimeError, all non-fatal
        logging.info("bundle provisioning: could not register login agent: %s", e)

    return actions


def auto(cmd: str) -> None:
    """Provision on the way into a CLI command, silently. Called for every
    command not in SKIP_COMMANDS."""
    if cmd in SKIP_COMMANDS:
        return
    # The stamp alone would skip a machine still carrying an older LaunchAgent at
    # the same version (a plist that reopens the app the moment it's quit), so
    # the plist gets a look too. Both checks are local file reads.
    if _stamp_is_current() and agent.gui_plist_is_current():
        return
    try:
        bootstrap()
    except Exception:  # noqa: BLE001 — belt and braces; this must never block a command
        logging.exception("auto-bootstrap failed (ignored)")
