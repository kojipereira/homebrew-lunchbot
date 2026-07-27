"""User-facing I/O: native macOS dialogs/alerts + terminal prompts.

The dialog helpers foreground themselves with `tell application "System Events"
to activate`, which is Automation-TCC-gated. On a machine where consent was
never granted the Apple Event fails with error -1743; we detect that, log it
distinctly, and fall back to a plain dialog (which still renders, just may not
steal focus) instead of silently abandoning the order.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config, Favorite

TCC_MARKERS = ("-1743", "not authorized to send apple events")


def _osascript(script: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-"], input=script,
                          capture_output=True, text=True, timeout=timeout)


def _is_tcc_denied(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(m in s for m in TCC_MARKERS)


def tcc_probe() -> tuple[bool, str]:
    """Fire a benign System Events activate to force/detect the Automation
    consent. Returns (granted, detail). Run this during attended setup."""
    r = _osascript('tell application "System Events" to activate', timeout=30)
    if r.returncode == 0:
        return True, "granted"
    if _is_tcc_denied(r.stderr):
        return False, (
            "Automation permission denied (error -1743). Grant it in "
            "System Settings → Privacy & Security → Automation → (your python) "
            "→ enable System Events, then re-run."
        )
    return False, (r.stderr or "unknown osascript error").strip()


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_dialog_body(fav: "Favorite", items: list[dict],
                      line_items: list[tuple[str, str]], total_cents: int,
                      fulfillment: str, budget: dict | None) -> str:
    item_lines = []
    for it in items:
        name = it.get("item", {}).get("name") or it.get("name") or "?"
        qty = it.get("quantity", 1)
        price = ((it.get("unit_price_monetary_fields") or {}).get("display_string")) or ""
        suffix = f" — {price}" if price else ""
        item_lines.append(f"• {qty}× {name}{suffix}")
    breakdown = [f"{lbl}: {disp}" for lbl, disp in line_items]
    budget_footer = ""
    if budget:
        remaining = ((budget.get("remaining_amount") or {}).get("display_string")) or "?"
        budget_footer = f"\n\nCompany benefit: {budget.get('name', '?')} ({remaining} remaining)"
    return (
        f"{fav.store} — ${total_cents/100:.2f} ({fulfillment.lower() or 'unknown'})\n\n"
        + "\n".join(item_lines)
        + ("\n\n" + "\n".join(breakdown) if breakdown else "")
        + budget_footer
    )


def desktop_confirm(cfg: "Config", fav: "Favorite", items: list[dict],
                    line_items: list[tuple[str, str]], total_cents: int,
                    fulfillment: str, budget: dict | None, allow_next: bool,
                    place_label: str = "Place") -> str:
    """Return 'Place' | 'Skip' | 'Next' | 'TIMEOUT'."""
    dc = cfg.desktop_confirm
    body = _esc(build_dialog_body(fav, items, line_items, total_cents, fulfillment, budget))
    buttons = (f'{{"Cancel", "Shuffle", "{place_label}"}}' if allow_next
               else f'{{"Cancel", "{place_label}"}}')
    core = (
        f'set r to display dialog "{body}" '
        f'buttons {buttons} default button "{place_label}" '
        f'with title "Lunchbot" with icon note '
        f'giving up after {dc.timeout_seconds}\n'
        'if gave up of r then return "TIMEOUT"\n'
        'return button returned of r'
    )
    r = _osascript('tell application "System Events" to activate\n' + core,
                   timeout=dc.timeout_seconds + 30)
    if r.returncode != 0 and _is_tcc_denied(r.stderr):
        logging.warning("Automation TCC denied (-1743); retrying dialog without activate. "
                        "Grant Automation→System Events to foreground reliably.")
        r = _osascript(core, timeout=dc.timeout_seconds + 30)
    if r.returncode != 0:
        logging.error("osascript failed: %s", (r.stderr or "").strip())
        return "Skip"
    raw = r.stdout.strip()
    logging.info("desktop_confirm: %s", raw)
    if raw == place_label:
        return "Place"
    if raw == "Cancel":
        return "Skip"
    if raw in ("Shuffle", "TIMEOUT"):
        return raw
    return "Skip"


def ask_retry(title: str, body: str, timeout: int = 300) -> bool:
    """Show an error with Cancel / Try again. Returns True if the user chose
    Try again (default button). A timeout or Cancel returns False."""
    core = (
        f'set r to display dialog "{_esc(body)}" '
        f'buttons {{"Cancel", "Try again"}} default button "Try again" '
        f'with title "{_esc(title)}" with icon caution giving up after {timeout}\n'
        'if gave up of r then return "TIMEOUT"\n'
        'return button returned of r'
    )
    r = _osascript('tell application "System Events" to activate\n' + core, timeout=timeout + 30)
    if r.returncode != 0 and _is_tcc_denied(r.stderr):
        r = _osascript(core, timeout=timeout + 30)
    if r.returncode != 0:
        return False
    choice = r.stdout.strip()
    logging.info("ask_retry: %s", choice)
    return choice == "Try again"


def show_alert(title: str, body: str) -> None:
    """Blocking informational alert. Always renders (display alert is not
    TCC-gated); falls back without activate if Automation is denied."""
    core = (f'display alert "{_esc(title)}" message "{_esc(body)}" '
            'as informational giving up after 120')
    r = _osascript('tell application "System Events" to activate\n' + core, timeout=140)
    if r.returncode != 0 and _is_tcc_denied(r.stderr):
        _osascript(core, timeout=140)


def notify(title: str, body: str) -> None:
    """Best-effort banner. Frequently dropped from a launchd agent — never
    rely on this for must-see output; use show_alert or the log instead."""
    script = f'display notification "{_esc(body)}" with title "{_esc(title)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=15)
    except Exception:
        pass


# ---- Terminal prompt helpers (wizard) --------------------------------------
def _in(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def ask(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    val = _in(f"{text}{suffix}: ").strip()
    return val or (default or "")


def ask_bool(text: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    val = _in(f"{text} ({d}): ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def ask_int(text: str, default: int) -> int:
    while True:
        val = _in(f"{text} [{default}]: ").strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            print("  please enter a number")


def ask_choice(text: str, choices: list[str], default: str) -> str:
    opts = "/".join(choices)
    while True:
        val = _in(f"{text} ({opts}) [{default}]: ").strip().lower()
        if not val:
            return default
        if val in choices:
            return val
        print(f"  choose one of: {opts}")


def ask_multiselect(text: str, labels: list[str],
                    pre_selected: list[int] | None = None) -> list[int]:
    """Show a numbered list, return selected 0-based indices.
    Accepts '1,3,5', ranges '1-4', 'all', or empty for pre_selected/none."""
    print(text)
    pre = set(pre_selected or [])
    for i, lab in enumerate(labels, 1):
        mark = "*" if (i - 1) in pre else " "
        print(f"  {mark} {i:2}. {lab}")
    default_hint = ",".join(str(i + 1) for i in sorted(pre)) if pre else ""
    raw = _in(f"Select (e.g. 1,3,5 or 1-4 or 'all') [{default_hint}]: ").strip().lower()
    if not raw:
        return sorted(pre) if pre else []
    if raw == "all":
        return list(range(len(labels)))
    picked: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                for n in range(int(a), int(b) + 1):
                    if 1 <= n <= len(labels):
                        picked.add(n - 1)
            except ValueError:
                continue
        elif part.isdigit():
            n = int(part)
            if 1 <= n <= len(labels):
                picked.add(n - 1)
    return sorted(picked)


def out(msg: str = "") -> None:
    print(msg, file=sys.stderr if not sys.stdout.isatty() else sys.stdout)
