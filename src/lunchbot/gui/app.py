"""Menu-bar app (rumps). Runs from the Homebrew venv, kept alive by the
com.lunchbot.gui LaunchAgent. Every action delegates to the same functions the
CLI uses — this file is pure presentation.

rumps is imported lazily so the module stays importable (and the package stays
testable) on a plain stdlib interpreter that has no rumps installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta

from .. import agent, setup_core
from ..config import ConfigError, favorite_eligible, load_config
from ..state import add_skip_date, already_ordered_today, load_state

try:
    import rumps
except ImportError:  # allows `import lunchbot.gui.app` without the dep
    rumps = None

APP_TITLE = "🥪"  # emoji fallback if the Lucide SVG can't be loaded
# Lucide "sandwich" icon, rendered by NSImage at runtime as a menu-bar template.
ICON_PATH = os.path.join(os.path.dirname(__file__), "icons", "sandwich.svg")
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
    return f"Next: {DAY_NAMES[nf.weekday() + 1]} {nf.strftime('%H:%M')}"


def _spawn_prefs() -> None:
    """Launch the Tkinter preferences form as its own process (AppKit's run
    loop and Tk's mainloop cannot share one process)."""
    subprocess.Popen([sys.executable, "-m", "lunchbot.gui.prefs"])


def _open_logs() -> None:
    from .. import paths
    subprocess.Popen(["open", str(paths.LOG_PATH)])


def _icon_loads(path: str) -> bool:
    """True only if the file exists AND NSImage can render it to a real image —
    guards against an invisible menu-bar item when SVG rendering isn't available."""
    if not os.path.exists(path):
        return False
    try:
        from AppKit import NSImage
        img = NSImage.alloc().initWithContentsOfFile_(path)
        return bool(img and img.isValid() and img.size().width > 0)
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    if rumps is None:
        print("The menu-bar app needs rumps (installed in the Homebrew venv). "
              "Run `lunchbot doctor` for details.", file=sys.stderr)
        return 1

    class LunchbotApp(rumps.App):
        def __init__(self):
            # Prefer the Lucide SVG, but only if NSImage can actually render it —
            # a nil/zero-size icon would make the menu-bar item invisible. Always
            # keep the emoji as `title` too, so SOMETHING shows no matter what.
            if _icon_loads(ICON_PATH):
                super().__init__("Lunchbot", icon=ICON_PATH, template=True, quit_button=None)
            else:
                super().__init__("Lunchbot", title=APP_TITLE, quit_button=None)
            self.status_item = rumps.MenuItem("…")
            self.status_item.set_callback(None)  # non-clickable status line
            self.order_menu = rumps.MenuItem("Order now")
            self.pause_item = rumps.MenuItem("Pause", callback=self.toggle_pause)
            self.menu = [
                self.status_item,
                None,
                self.order_menu,
                rumps.MenuItem("Skip today", callback=self.skip_today),
                self.pause_item,
                None,
                rumps.MenuItem("Preferences…", callback=lambda _: _spawn_prefs()),
                rumps.MenuItem("View logs", callback=lambda _: _open_logs()),
                None,
                rumps.MenuItem("Quit Lunchbot", callback=rumps.quit_application),
            ]
            self.refresh(None)
            rumps.Timer(self.refresh, 60).start()
            # Force menu-bar-accessory mode shortly after the run loop is up, so
            # the 🥪 icon reliably appears top-right with no Dock icon — however
            # the process was launched (double-click app, launchd, or CLI).
            self._policy_timer = rumps.Timer(self._ensure_accessory, 0.3)
            self._policy_timer.start()
            # First run: no config yet → open Preferences so setup is obvious
            # instead of leaving the user staring at a "Not set up" menu.
            try:
                load_config()
            except ConfigError:
                _spawn_prefs()

        def _ensure_accessory(self, timer):
            try:
                from AppKit import NSApp, NSApplicationActivationPolicyAccessory
                NSApp().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            except Exception:  # noqa: BLE001 — cosmetic; never block the app
                pass
            timer.stop()

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
            if getattr(self.order_menu, "_menu", None) is not None:
                self.order_menu.clear()
            try:
                cfg = load_config()
                favs = [f for f in cfg.favorites if favorite_eligible(f.diet, cfg.diet)]
            except ConfigError:
                favs = []
            if not favs:
                placeholder = rumps.MenuItem("(set up restaurants first)")
                placeholder.set_callback(None)
                self.order_menu.add(placeholder)
                return
            for f in favs:
                self.order_menu.add(
                    rumps.MenuItem(f.store, callback=self._make_order_cb(f.store)))

        # ---- actions -----------------------------------------------------
        def _make_order_cb(self, store):
            def cb(_):
                threading.Thread(target=self._order_worker, args=(store,),
                                 daemon=True).start()
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

        def toggle_pause(self, _):
            try:
                cfg = load_config()
            except ConfigError as e:
                rumps.alert("Lunchbot", f"Config problem: {e}")
                return
            if agent.is_loaded():
                agent.pause_agent()
                rumps.notification("Lunchbot", "", "Schedule paused.")
            else:
                try:
                    agent.resume_agent(cfg)
                    rumps.notification("Lunchbot", "", "Schedule resumed.")
                except RuntimeError as e:
                    rumps.alert("Lunchbot", f"Couldn't resume: {e}")
            self.refresh(None)

    LunchbotApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
