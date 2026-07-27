"""Mutable runtime state — the only thing lunchbot writes after setup.

Holds the rotation cursor, the log of placed orders, and skip_dates.
Kept out of config.toml so the hand-rolled TOML writer never has to
round-trip (which would be lossy for comments + [[favorites]] tables).
"""

from __future__ import annotations

import json
from datetime import date

from . import paths


def load_state() -> dict:
    if paths.STATE_PATH.exists():
        try:
            data = json.loads(paths.STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    data.setdefault("cursor", 0)
    data.setdefault("orders", {})
    data.setdefault("skip_dates", [])
    # Whether the user has paused the ordering schedule. The menu-bar app stops
    # the schedule when it closes and restores it on launch — but only back to
    # this last-known intent, so a deliberate pause survives a quit/reopen.
    data.setdefault("schedule_paused", False)
    return data


def save_state(state: dict) -> None:
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    paths.STATE_PATH.write_text(json.dumps(state, indent=2))


def already_ordered_today(state: dict) -> bool:
    return date.today().isoformat() in state.get("orders", {})


def set_schedule_paused(paused: bool) -> None:
    """Persist the user's schedule intent (paused vs. active)."""
    state = load_state()
    state["schedule_paused"] = bool(paused)
    save_state(state)


def add_skip_date(iso_date: str) -> bool:
    """Add a date to skip_dates. Returns True if newly added."""
    state = load_state()
    if iso_date in state["skip_dates"]:
        return False
    state["skip_dates"].append(iso_date)
    save_state(state)
    return True
