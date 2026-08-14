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
    # {iso_date: store} — forces a specific favorite on a future scheduled run
    # instead of the pool-rotation pick. Left in place once the date passes;
    # nothing ever checks a stale date again, so there's nothing to clean up.
    data.setdefault("overrides", {})
    # Whether the user has paused the ordering schedule. The menu-bar app stops
    # the schedule when it closes and restores it on launch — but only back to
    # this last-known intent, so a deliberate pause survives a quit/reopen.
    data.setdefault("schedule_paused", False)
    # {"date": iso, "store": name} — Yolo mode's restaurant for that day, drawn
    # once and reused by every fire of the day (see run.daily_pick). One entry,
    # not a map: it's stamped with its date and means nothing the next morning.
    data.setdefault("daily_pick", {})
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


def set_override(iso_date: str, store: str) -> None:
    """Force `store` on iso_date's scheduled run instead of the pool-rotation
    pick. Still subject to the normal weekday/skip/already-ordered gates —
    this only changes *which* favorite gets tried, not whether the day fires."""
    state = load_state()
    state["overrides"][iso_date] = store
    save_state(state)


def clear_override(iso_date: str) -> bool:
    """Undo a set_override for iso_date. Returns True if one was removed."""
    state = load_state()
    if state["overrides"].pop(iso_date, None) is None:
        return False
    save_state(state)
    return True


def get_override(iso_date: str) -> str | None:
    return load_state()["overrides"].get(iso_date)


# ---- Yolo mode's restaurant of the day -------------------------------------
# These two take the caller's state dict instead of loading a fresh one (the
# pattern add_skip_date/set_override use). `run` holds a state dict for the
# whole run and hands it to submit_and_record, which saves it — a load/save
# helper would write the pick and then have it overwritten by that save.
def get_daily_pick(state: dict, iso_date: str) -> str | None:
    """The store already drawn for iso_date, or None (including when the stored
    pick belongs to an earlier day)."""
    entry = state.get("daily_pick") or {}
    return entry.get("store") if entry.get("date") == iso_date else None


def set_daily_pick(state: dict, iso_date: str, store: str) -> None:
    state["daily_pick"] = {"date": iso_date, "store": store}
    save_state(state)
