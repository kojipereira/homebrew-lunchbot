"""Menu-bar app (rumps). Runs from the Homebrew venv, started at login by the
com.lunchbot.gui LaunchAgent. Every action delegates to the same functions the
CLI uses — this file is pure presentation.

Only one copy ever runs (see [singleton.py]): the login agent, a double-click on
Lunchbot.app and `lunchbot gui` all come through main(), and whichever loses the
race hands its request to the winner and exits. `--prefs` means "and open the
preferences window", which is what opening Lunchbot.app does.

rumps is imported lazily so the module stays importable (and the package stays
testable) on a plain stdlib interpreter that has no rumps installed.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta

from .. import agent, bootstrap, paths, setup_core, singleton
from ..config import ConfigError, load_config
from ..state import (add_skip_date, already_ordered_today, clear_override,
                     get_override, load_state, set_override, set_schedule_paused)

try:
    import rumps
except ImportError:  # allows `import lunchbot.gui.app` without the dep
    rumps = None

APP_TITLE = "🥪"  # emoji fallback if the icon can't be loaded — see LunchbotApp.__init__
ICON_PATH = os.path.join(os.path.dirname(__file__), "icons", "lunchbot.pdf")
DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


def next_fire(cfg) -> datetime | None:
    """Soonest upcoming (weekday, hour, minute) slot from now, or None."""
    times = agent.fire_times(cfg)
    if not times or not cfg.weekdays:
        return None
    now = datetime.now()
    cands: list[datetime] = []
    for offset in range(0, 8):
        d = now + timedelta(days=offset)
        if (d.weekday() + 1) not in cfg.weekdays:
            continue
        for (h, m) in times:
            c = d.replace(hour=h, minute=m, second=0, microsecond=0)
            if c > now:
                cands.append(c)
    return min(cands) if cands else None


def _status_text() -> str:
    try:
        cfg = load_config()
    except ConfigError:
        return "Not set up — open Preferences…"
    state = load_state()
    if already_ordered_today(state):
        o = state.get("orders", {}).get(datetime.now().date().isoformat(), {})
        return f"Ordered today ✓  {o.get('store', '')}".strip()
    if not agent.is_loaded():
        return "Paused" if agent.PLIST_PATH.exists() else "Schedule not installed"
    nf = next_fire(cfg)
    if not nf:
        return "No eligible restaurants scheduled"
    base = f"Next: {DAY_NAMES[nf.weekday() + 1]} {nf.strftime('%H:%M')}"
    override = get_override(nf.date().isoformat())
    return f"{base} → {override}" if override else base


def _stop_schedule_quietly() -> None:
    """Unload the ordering schedule (best effort) so nothing fires while the
    app is closed. The user's paused/active intent is untouched, so the next
    launch restores the same schedule. Safe to call more than once."""
    try:
        agent.stop_agent()
    except Exception:  # noqa: BLE001 — must never block app shutdown
        logging.exception("stopping schedule on exit failed (ignored)")


def _restore_schedule_quietly() -> None:
    """Reinstall the ordering schedule from the current config unless the user
    last left it paused. Best effort; safe to call off the main thread."""
    try:
        cfg = load_config()
    except ConfigError:
        return  # not set up yet; nothing to restore
    if load_state().get("schedule_paused"):
        return  # honour a deliberate pause across quit/reopen
    try:
        agent.resume_agent(cfg)  # = install_agent: restart last schedule
    except Exception:  # noqa: BLE001 — never block startup
        logging.exception("restoring schedule on launch failed (ignored)")


def _spawn_prefs() -> None:
    """Launch the preferences window as its own process. It's AppKit too, but it
    wants a regular activation policy and its own NSApplication run loop —
    neither of which this menu-bar accessory can hand over."""
    subprocess.Popen([sys.executable, "-m", "lunchbot.gui.prefs"])


def _open_logs() -> None:
    subprocess.Popen(["open", str(paths.LOG_PATH)])


def _icon_loads(path: str) -> bool:
    """True only if the file exists AND NSImage can render it to a real image —
    guards against an invisible menu-bar item if the icon can't be decoded."""
    if not os.path.exists(path):
        return False
    try:
        from AppKit import NSImage
        img = NSImage.alloc().initWithContentsOfFile_(path)
        return bool(img and img.isValid() and img.size().width > 0)
    except Exception:  # noqa: BLE001
        return False


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lunchbot-gui", description="Lunchbot's menu-bar app.")
    p.add_argument("--prefs", action="store_true",
                   help="open Preferences as well as the menu-bar icon "
                        "(what opening Lunchbot.app does)")
    return p.parse_args(argv)


# Module-level so the lock lives as long as the process; see singleton.py.
_instance_lock = None


def main(argv=None) -> int:
    args = _parse_args(argv)
    # The `lunchbot-gui` console script comes straight here, bypassing the CLI's
    # setup, so the app's own logging (including the decision below) would
    # otherwise go nowhere. Idempotent.
    paths.setup_logging()

    # One menu bar, one sandwich. Login (launchd), Lunchbot.app and `lunchbot
    # gui` can all land here, so a copy that isn't the first steps aside rather
    # than adding a second icon — after handing over what the user asked for.
    global _instance_lock
    _instance_lock = singleton.InstanceLock("gui")
    if not _instance_lock.acquire():
        logging.info("menu-bar app already running; not starting a second copy")
        if args.prefs:
            _spawn_prefs()          # the running icon stays; just show the window
        else:
            print("Lunchbot is already running — look for 🥪 in the menu bar.",
                  file=sys.stderr)
        return 0                    # a clean exit: launchd must not relaunch us

    if rumps is None:
        print("The menu-bar app needs rumps (installed in the Homebrew venv). "
              "Run `lunchbot doctor` for details.", file=sys.stderr)
        return 1

    # Stop the schedule whenever this process exits — covers the Quit menu item
    # (which also stops it up front) plus any other clean shutdown path.
    atexit.register(_stop_schedule_quietly)

    class LunchbotApp(rumps.App):
        def __init__(self, open_prefs=False):
            # A prior SVG-as-template attempt came out blank in the menu bar
            # despite NSImage reporting a valid, non-zero-size image, on the
            # exact launch path the app took at the time: LaunchServices
            # (Finder double-click / `open`) exec'ing straight into the
            # unsigned bundle's binary. That path no longer exists — see
            # appbundle.py's launcher, which now kickstarts the registered
            # login agent instead — so a real logo (PDF, not SVG) is worth
            # trying again. _icon_loads() still guards against a genuinely
            # undecodable file; the emoji title stays as a fallback either way.
            if _icon_loads(ICON_PATH):
                super().__init__("Lunchbot", icon=ICON_PATH, template=True, quit_button=None)
            else:
                super().__init__("Lunchbot", title=APP_TITLE, quit_button=None)
            self.status_item = rumps.MenuItem("…")
            self.status_item.set_callback(None)  # non-clickable status line
            self.order_menu = rumps.MenuItem("Order now")
            self.order_tomorrow_menu = rumps.MenuItem("Order tomorrow")
            self.pause_item = rumps.MenuItem("Pause", callback=self.toggle_pause)
            self.menu = [
                self.status_item,
                None,
                self.order_menu,
                self.order_tomorrow_menu,
                rumps.MenuItem("Skip today", callback=self.skip_today),
                rumps.MenuItem("Skip tomorrow", callback=self.skip_tomorrow),
                self.pause_item,
                None,
                rumps.MenuItem("Preferences…", callback=lambda _: _spawn_prefs()),
                rumps.MenuItem("View logs", callback=lambda _: _open_logs()),
                None,
                rumps.MenuItem("Quit Lunchbot", callback=self._on_quit),
            ]
            self.refresh(None)
            rumps.Timer(self.refresh, 60).start()
            # Restore the schedule to its last state on launch (off the main
            # thread so launchctl never stalls the menu appearing), then refresh
            # once it's settled.
            threading.Thread(target=self._restore_schedule, daemon=True).start()
            self._startup_refresh = rumps.Timer(self._refresh_once, 2)
            self._startup_refresh.start()
            # Force menu-bar-accessory mode shortly after the run loop is up, so
            # the 🥪 icon reliably appears top-right with no Dock icon — however
            # the process was launched (double-click app, launchd, or CLI).
            self._policy_timer = rumps.Timer(self._ensure_accessory, 0.3)
            self._policy_timer.start()
            # Opening Lunchbot from Finder/Launchpad/the Dock means "show me
            # Lunchbot", so it lands in the menu bar *and* opens Preferences.
            # Same on a first run with no config yet, whoever started us —
            # better than leaving the user staring at a "Not set up" menu.
            if open_prefs:
                _spawn_prefs()
            else:
                try:
                    load_config()
                except ConfigError:
                    _spawn_prefs()

        def _ensure_accessory(self, timer):
            try:
                from AppKit import NSApp, NSApplicationActivationPolicyAccessory
                # Launched from Lunchbot.app, LSUIElement=true already starts us
                # as Accessory — re-applying the same policy is a no-op that's
                # been observed to disrupt an already-rendered status item on
                # some macOS/pyobjc combinations. Only transition when needed
                # (the raw `python -m lunchbot.gui.app` path, which starts Regular).
                if NSApp().activationPolicy() != NSApplicationActivationPolicyAccessory:
                    NSApp().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            except Exception:  # noqa: BLE001 — cosmetic; never block the app
                pass
            timer.stop()

        # ---- lifecycle ---------------------------------------------------
        def _restore_schedule(self):
            """On launch, bring the ordering schedule back to its last state.
            Runs on a background thread — never touch rumps UI here."""
            # A dragged-in Lunchbot.app reaches this file directly, without ever
            # passing through the CLI's bootstrap.auto(), so this is the only
            # place that registers its login agent. No-op for Homebrew installs.
            try:
                for line in bootstrap.provision_from_bundle():
                    logging.info("bundle provisioning: %s", line)
            except Exception:  # noqa: BLE001 — never block startup
                logging.exception("bundle provisioning failed (ignored)")
            _restore_schedule_quietly()

        def _refresh_once(self, timer):
            timer.stop()
            self.refresh(None)

        def _on_quit(self, sender):
            # Exits cleanly, which is exactly what the LaunchAgent's
            # KeepAlive/SuccessfulExit key watches for: quit stays quit until the
            # next login (or a click on Lunchbot.app).
            _stop_schedule_quietly()
            rumps.quit_application(sender)

        # ---- refresh -----------------------------------------------------
        def refresh(self, _):
            # Never let a transient error (dd-cli hiccup, launchctl race) escape
            # the timer callback — an unhandled exception would tear down the run
            # loop and the menu-bar icon with it.
            try:
                self.status_item.title = _status_text()
                loaded = agent.is_loaded()
                self.pause_item.title = "Resume" if (not loaded and agent.PLIST_PATH.exists()) else "Pause"
                self._rebuild_order_menu()
            except Exception:  # noqa: BLE001
                import logging
                logging.exception("menu refresh failed (ignored)")

        def _rebuild_order_menu(self):
            # A rumps submenu has no backing NSMenu until its first item is
            # added, so clear() would raise on the initial build — guard it.
            for menu in (self.order_menu, self.order_tomorrow_menu):
                if getattr(menu, "_menu", None) is not None:
                    menu.clear()
            try:
                cfg = load_config()
                favs = list(cfg.favorites)
            except ConfigError:
                favs = []
            if not favs:
                for menu in (self.order_menu, self.order_tomorrow_menu):
                    placeholder = rumps.MenuItem("(set up restaurants first)")
                    placeholder.set_callback(None)
                    menu.add(placeholder)
                return
            # Mark whichever favorite (if any) is already queued for tomorrow,
            # right where you'd go to change it — otherwise it's an invisible
            # state that's easy to set and forget.
            tomorrow_iso = (datetime.now().date() + timedelta(days=1)).isoformat()
            tomorrow_override = get_override(tomorrow_iso)
            if tomorrow_override:
                self.order_tomorrow_menu.add(rumps.MenuItem(
                    "Clear override", callback=self._make_clear_tomorrow_cb()))
            for f in favs:
                self.order_menu.add(
                    rumps.MenuItem(f.store, callback=self._make_order_cb(f.store)))
                label = f"✓ {f.store}" if f.store == tomorrow_override else f.store
                self.order_tomorrow_menu.add(
                    rumps.MenuItem(label, callback=self._make_order_tomorrow_cb(f.store)))

        # ---- actions -----------------------------------------------------
        def _make_order_cb(self, store):
            def cb(_):
                threading.Thread(target=self._order_worker, args=(store,),
                                 daemon=True).start()
            return cb

        def _make_order_tomorrow_cb(self, store):
            # Just a local state write (no dd-cli/network call) — safe on the
            # main thread, unlike _order_worker which places a real order now.
            def cb(_):
                iso = (datetime.now().date() + timedelta(days=1)).isoformat()
                set_override(iso, store)
                rumps.notification("Lunchbot", "",
                                   f"Tomorrow: will order {store} instead of the usual pick.")
            return cb

        def _make_clear_tomorrow_cb(self):
            def cb(_):
                iso = (datetime.now().date() + timedelta(days=1)).isoformat()
                if clear_override(iso):
                    rumps.notification("Lunchbot", "", "Tomorrow: back to the usual rotation.")
            return cb

        def _order_worker(self, store):
            # Runs on a BACKGROUND thread: never touch rumps/AppKit UI here (that
            # crashes the app and drops the menu-bar icon). Use show_alert, which
            # shells out to osascript and is safe from any thread. Let the 60s
            # timer handle the menu refresh on the main thread.
            from ..run import run
            from ..ui import show_alert
            try:
                cfg = load_config()
            except ConfigError as e:
                show_alert("Lunchbot", f"Config problem: {e}")
                return
            ready = setup_core.preflight(attempt_login=False)
            if not ready.ok:
                show_alert("dd-cli not ready", ready.detail)
                return
            try:
                run(cfg, load_state(), force_pick=store, dry_run_override=cfg.dry_run)
            except Exception as e:  # noqa: BLE001 — surface, never crash the app
                show_alert("Lunchbot: order failed", str(e))

        def skip_today(self, _):
            iso = datetime.now().date().isoformat()
            msg = "Skipped today." if add_skip_date(iso) else "Today was already skipped."
            rumps.notification("Lunchbot", "", msg)
            self.refresh(None)

        def skip_tomorrow(self, _):
            iso = (datetime.now().date() + timedelta(days=1)).isoformat()
            msg = "Skipped tomorrow." if add_skip_date(iso) else "Tomorrow was already skipped."
            rumps.notification("Lunchbot", "", msg)
            self.refresh(None)

        def toggle_pause(self, _):
            try:
                cfg = load_config()
            except ConfigError as e:
                rumps.alert("Lunchbot", f"Config problem: {e}")
                return
            if agent.is_loaded():
                agent.pause_agent()
                set_schedule_paused(True)
                rumps.notification("Lunchbot", "", "Schedule paused.")
            else:
                try:
                    agent.resume_agent(cfg)
                    set_schedule_paused(False)
                    rumps.notification("Lunchbot", "", "Schedule resumed.")
                except RuntimeError as e:
                    rumps.alert("Lunchbot", f"Couldn't resume: {e}")
            self.refresh(None)

    LunchbotApp(open_prefs=args.prefs).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
