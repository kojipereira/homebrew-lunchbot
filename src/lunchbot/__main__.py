"""CLI dispatch: setup | run | gui | prefs | bootstrap | install-agent |
uninstall-agent | install-gui-agent | uninstall-gui-agent | pause | resume |
test | skip | override | status | logs | doctor."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from . import agent, bootstrap, ddcli, paths
from .config import ConfigError, load_config
from .state import add_skip_date, load_state, set_override, set_schedule_paused


def _load_cfg_or_die():
    try:
        return load_config()
    except ConfigError as e:
        from .ui import show_alert
        logging.error("config error: %s", e)
        show_alert(
            "Lunchbot needs setup",
            f"{e}\n\nOpen Lunchbot Preferences or run `lunchbot setup`.\n"
            f"Config file: {paths.CONFIG_PATH}",
        )
        print(str(e), file=sys.stderr)
        raise SystemExit(2)


def _cmd_run(args) -> int:
    from .run import run
    from .ui import ask_retry, ask_sign_in, notify, show_alert
    cfg = _load_cfg_or_die()
    state = load_state()
    retried = False
    sign_in_attempted = False
    while True:
        try:
            run(cfg, state, force_pick=args.pick, dry_run_override=args.dry_run)
            return 0
        except ddcli.NotLoggedIn as e:
            logging.warning("DoorDash sign-in required: %s", e)
            if sign_in_attempted or not ask_sign_in(
                    "Your DoorDash session has expired. Lunchbot can open the "
                    "sign-in flow now, then retry this order."):
                show_alert(
                    "Lunchbot couldn't sign in",
                    "Sign in from the Lunchbot prompt, then try again.\n\n"
                    f"If it still fails, run `lunchbot doctor`. Log: {paths.LOG_PATH}",
                )
                return 1
            sign_in_attempted = True
            if ddcli.login_interactive():
                try:
                    ddcli.login_probe()
                    continue
                except ddcli.DdError:
                    logging.exception("DoorDash sign-in did not restore access")
            show_alert(
                "Lunchbot couldn't sign in",
                "Finish the DoorDash sign-in in your browser, then try again.\n\n"
                f"If it still fails, run `lunchbot doctor`. Log: {paths.LOG_PATH}",
            )
            return 1
        except Exception:  # noqa: BLE001 — top-level guard, must never crash silently
            logging.exception("run failed")
            if not retried and ask_retry(
                    "Lunchbot needs attention",
                    "Lunchbot couldn't complete this order. Try again?\n\n"
                    f"Details were saved to: {paths.LOG_PATH}"):
                retried = True
                continue
            show_alert(
                "Lunchbot couldn't complete the order",
                "Try again from the Lunchbot menu. If it happens again, run "
                f"`lunchbot doctor`.\n\nLog: {paths.LOG_PATH}",
            )
            notify("Lunchbot needs attention", f"See log: {paths.LOG_PATH}")
            return 1


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
    overrides = state.get("overrides", {})
    upcoming = {d: s for d, s in overrides.items() if d >= date.today().isoformat()}
    if upcoming:
        print("upcoming overrides: " + ", ".join(f"{d}={upcoming[d]}" for d in sorted(upcoming)))
    pick = state.get("daily_pick") or {}
    if pick.get("date") == date.today().isoformat():
        print(f"today's pick: {pick.get('store')} (Yolo mode)")
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


def _cmd_override(args) -> int:
    cfg = _load_cfg_or_die()
    if not any(f.store.lower() == args.store.lower() for f in cfg.favorites):
        names = ", ".join(f.store for f in cfg.favorites)
        print(f"no favorite named {args.store!r} — have: {names}", file=sys.stderr)
        return 2
    d = args.date or (date.today() + timedelta(days=1)).isoformat()
    set_override(d, args.store)
    print(f"{d}: will order {args.store} instead of the usual rotation pick")
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
    p_gui = sub.add_parser("gui", help="run the menu-bar app (foreground)")
    p_gui.add_argument("--prefs", action="store_true",
                       help="also open the preferences window (what Lunchbot.app does)")
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
    p_override = sub.add_parser(
        "override", help="force a favorite on a future date (default: tomorrow)")
    p_override.add_argument("store", help="favorite name, e.g. 'Joe's Diner'")
    p_override.add_argument("date", nargs="?", help="YYYY-MM-DD (default tomorrow)")

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
        from .gui import app as gui_app
        return gui_app.main(["--prefs"] if args.prefs else [])
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
        # Record the intent too, or the menu-bar app's next launch would see an
        # un-paused state and restore the schedule out from under this pause.
        set_schedule_paused(True)
        print("agent paused")
        return 0
    if cmd == "resume":
        agent.resume_agent(_load_cfg_or_die())
        set_schedule_paused(False)
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
    if cmd == "skip":
        return _cmd_skip(args)
    if cmd == "override":
        return _cmd_override(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
