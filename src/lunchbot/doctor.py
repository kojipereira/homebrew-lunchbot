"""`lunchbot doctor` — verify everything a colleague needs, surfacing the
first-run cliffs that are invisible to someone who already granted consent."""

from __future__ import annotations

import subprocess
import sys

from . import agent, ddcli, paths
from .config import ConfigError, favorite_eligible, load_config
from .ui import tcc_probe

OK, WARN, BAD = "✓", "!", "✗"

# Known-good dd-cli version this build was written against.
DDCLI_PIN = "0.2.0"


def _p(mark: str, msg: str) -> None:
    print(f"  {mark}  {msg}")


def doctor() -> int:
    print("lunchbot doctor\n")
    critical_ok = True

    # 1. Interpreter
    v = sys.version_info
    if v >= (3, 11):
        _p(OK, f"python {v.major}.{v.minor}.{v.micro}")
    else:
        critical_ok = False
        _p(BAD, f"python {v.major}.{v.minor} is too old (need ≥3.11). `brew install python@3.13`")

    # 2. dd-cli present
    exe = ddcli.which()
    if not exe:
        critical_ok = False
        _p(BAD, "dd-cli not on PATH")
        for line in ddcli.ddcli_get_instructions().splitlines():
            print(f"       {line}")
    else:
        _p(OK, f"dd-cli at {exe}")
        # 3. Quarantine / Gatekeeper
        if ddcli.is_quarantined(exe):
            critical_ok = False
            _p(BAD, f"dd-cli is quarantined — run: {ddcli.dequarantine_hint(exe)}")
        else:
            _p(OK, "dd-cli not quarantined")
        gk = ddcli.gatekeeper_status(exe)
        _p(WARN if "rejected" in gk.lower() else OK, f"Gatekeeper: {gk.splitlines()[0] if gk else 'unknown'}")
        # 4. Version pin
        ver = ddcli.version() or "unknown"
        _p(OK if DDCLI_PIN in ver else WARN,
           f"dd-cli version: {ver}" + ("" if DDCLI_PIN in ver else f" (built against {DDCLI_PIN})"))
        # 5. Login / TLS
        try:
            ddcli.login_probe()
            _p(OK, "dd-cli logged in")
        except ddcli.TlsError as e:
            critical_ok = False
            _p(BAD, str(e))
        except ddcli.NotLoggedIn as e:
            critical_ok = False
            _p(BAD, str(e))
        except ddcli.DdError as e:
            _p(WARN, f"login probe inconclusive: {e}")

    # 6. Automation TCC
    granted, detail = tcc_probe()
    _p(OK if granted else WARN, f"Automation (dialogs): {detail}")

    # 7. Config
    try:
        cfg = load_config()
        eligible = [f for f in cfg.favorites if favorite_eligible(f.diet, cfg.diet)]
        _p(OK, f"config OK — diet={cfg.diet}, {len(cfg.favorites)} favorites "
               f"({len(eligible)} eligible), lunch {cfg.lunch_time}")
        times = ", ".join(f"{h:02d}:{m:02d}" for h, m in agent.fire_times(cfg)) or "(none!)"
        _p(OK if agent.fire_times(cfg) else BAD, f"fire times: {times}")
    except ConfigError as e:
        critical_ok = False
        _p(BAD, f"config: {e}")

    # 8. Ordering agent
    _p(OK if agent.is_loaded() else WARN,
       "ordering agent loaded" if agent.is_loaded() else "ordering agent not loaded (`lunchbot install-agent`)")

    # 9. Menu-bar GUI stack (optional — never blocks ordering)
    try:
        import rumps  # noqa: F401
        _p(OK, "rumps present (menu-bar app can run)")
    except ImportError:
        _p(WARN, "rumps not installed — menu-bar app unavailable (install via Homebrew)")
    try:
        import tkinter  # noqa: F401
        _p(OK, "Tk present (preferences window can open)")
    except ImportError:
        _p(WARN, "Tk missing — preferences window unavailable (`brew install python-tk@3.13`)")
    _p(OK if agent.gui_is_loaded() else WARN,
       "menu-bar app running" if agent.gui_is_loaded() else "menu-bar app not running (`lunchbot install-gui-agent`)")

    # 10. Log dir writable
    try:
        paths.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _p(OK, f"log dir writable ({paths.LOG_PATH.parent})")
    except OSError as e:
        _p(WARN, f"log dir issue: {e}")

    print()
    print("All critical checks passed." if critical_ok else "Some critical checks FAILED — see ✗ above.")
    return 0 if critical_ok else 1
