"""CLI dispatch: setup | run | gui | prefs | bootstrap | install-agent |
uninstall-agent | install-gui-agent | uninstall-gui-agent | pause | resume |
test | skip | status | logs | doctor."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import agent, bootstrap, paths
from .config import ConfigError, load_config
from .state import add_skip_date, load_state


def _load_cfg_or_die():
    try:
        return load_config()
    except ConfigError as e:
        from .ui import show_alert
        logging.error("config error: %s", e)
        show_alert("Lunchbot: config error", str(e))
        print(str(e), file=sys.stderr)
        raise SystemExit(2)


def _cmd_run(args) -> int:
    from .run import run
    from .ui import notify
    cfg = _load_cfg_or_die()
    state = load_state()
    try:
        run(cfg, state, force_pick=args.pick, dry_run_override=args.dry_run)
    except Exception as e:  # noqa: BLE001 — top-level guard, must never crash silently
        logging.exception("run failed")
        from .ui import show_alert
        show_alert("Lunchbot failed", str(e))
        notify("Lunchbot failed", str(e))
        return 1
    return 0


def _cmd_status(_args) -> int:
    loaded = agent.is_loaded()
    print(f"agent: {'loaded' if loaded else 'NOT loaded'} ({agent.LABEL})")
    try:
        cfg = load_config()
        times = ", ".join(f"{h:02d}:{m:02d}" for h, m in agent.fire_times(cfg))
        wd = ",".join(str(d) for d in cfg.weekdays)
        print(f"schedule: {times or '(none)'} on weekdays {wd} (lunch {cfg.lunch_time})")
        print(f"dry_run: {cfg.dry_run}   work_benefits: {cfg.work_benefits}")
    except ConfigError as e:
        print(f"config: {e}")
    state = load_state()
    orders = state.get("orders", {})
    if orders:
        last_day = max(orders)
        o = orders[last_day]
        print(f"last order: {last_day} — {o.get('store')} ${o.get('total_cents',0)/100:.2f}")
    skips = state.get("skip_dates", [])
    if skips:
        print(f"skip dates: {', '.join(sorted(skips))}")
    return 0


def _cmd_logs(_args) -> int:
    if not paths.LOG_PATH.exists():
        print("no log yet")
        return 0
    lines = paths.LOG_PATH.read_text(errors="replace").splitlines()
    print("\n".join(lines[-50:]))
    return 0


def _cmd_skip(args) -> int:
    d = args.date or date.today().isoformat()
    print(f"added {d} to skip dates" if add_skip_date(d) else f"{d} already skipped")
    return 0


def main(argv=None) -> int:
    paths.setup_logging()
    parser = argparse.ArgumentParser(prog="lunchbot",
                                     description="Auto-order weekday lunch via dd-cli.")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="the daily job (launchd calls this)")
    p_run.add_argument("--dry-run", action="store_true", help="preview only, never submit")
    p_run.add_argument("--pick", type=str, help="force a favorite by store name")

    sub.add_parser("setup", help="interactive setup / edit preferences")
    sub.add_parser("gui", help="run the menu-bar app (foreground)")
    sub.add_parser("prefs", help="open the preferences window")
    sub.add_parser("doctor", help="health check")
    sub.add_parser("bootstrap", help="(re)create Lunchbot.app + the menu-bar agent")
    sub.add_parser("install-agent", help="install/refresh the launchd schedule")
    sub.add_parser("uninstall-agent", help="remove the launchd schedule")
    sub.add_parser("install-gui-agent", help="auto-start the menu-bar app at login")
    sub.add_parser("uninstall-gui-agent", help="stop auto-starting the menu-bar app")
    sub.add_parser("install-app", help="create a double-clickable Lunchbot.app in ~/Applications")
    sub.add_parser("uninstall-app", help="remove Lunchbot.app")
    sub.add_parser("pause", help="disable the schedule without uninstalling")
    sub.add_parser("resume", help="re-enable the schedule")
    sub.add_parser("test", help="fire the agent immediately")
    sub.add_parser("status", help="show schedule + last order")
    sub.add_parser("logs", help="tail the log")
    p_skip = sub.add_parser("skip", help="skip a date (default: today)")
    p_skip.add_argument("date", nargs="?", help="YYYY-MM-DD (default today)")

    args = parser.parse_args(argv)
    cmd = args.cmd or "setup"

    # A fresh `brew install` can't create ~/Applications/Lunchbot.app or the
    # menu-bar LaunchAgent itself (Homebrew's post_install sandbox denies $HOME),
    # so the first command after an install/upgrade does it. Silent and one-shot.
    bootstrap.auto(cmd)

    if cmd == "run":
        return _cmd_run(args)
    if cmd == "setup":
        from .wizard import setup
        return setup()
    if cmd == "gui":
        from .gui.app import main as gui_main
        return gui_main()
    if cmd == "prefs":
        from .gui.prefs import main as prefs_main
        return prefs_main()
    if cmd == "doctor":
        from .doctor import doctor
        return doctor()
    if cmd == "bootstrap":
        for line in bootstrap.bootstrap(force=True) or ["nothing to do"]:
            print(line)
        return 0
    if cmd == "install-agent":
        cfg = _load_cfg_or_die()
        agent.migrate_legacy()
        agent.install_agent(cfg)
        print("agent installed")
        return 0
    if cmd == "uninstall-agent":
        agent.uninstall_agent()
        print("agent removed")
        return 0
    if cmd == "install-gui-agent":
        agent.install_gui_agent()
        print("menu-bar app will start at login")
        return 0
    if cmd == "uninstall-gui-agent":
        agent.uninstall_gui_agent()
        print("menu-bar auto-start removed")
        return 0
    if cmd == "install-app":
        from . import appbundle
        app = appbundle.install_app()
        print(f"created {app}")
        print("Double-click it in Finder (first time: right-click → Open to clear Gatekeeper).")
        print("Drag it to your Dock or Desktop to keep it handy.")
        return 0
    if cmd == "uninstall-app":
        from . import appbundle
        print("removed Lunchbot.app" if appbundle.uninstall_app() else "no Lunchbot.app found")
        return 0
    if cmd == "pause":
        agent.pause_agent()
        print("agent paused")
        return 0
    if cmd == "resume":
        agent.resume_agent(_load_cfg_or_die())
        print("agent resumed")
        return 0
    if cmd == "test":
        agent.test_fire()
        print("fired (check `lunchbot logs`)")
        return 0
    if cmd == "status":
        return _cmd_status(args)
    if cmd == "logs":
        return _cmd_logs(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
