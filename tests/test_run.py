"""Scheduled-slot behavior tests. Run with:
    PYTHONPATH=src python3.13 tests/test_run.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot.config import Config, Favorite  # noqa: E402
from lunchbot import run as R  # noqa: E402
from lunchbot.run import has_later_slot  # noqa: E402


failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")
    else:
        print(f"ok:   {msg}")


cfg = Config(
    lunch_time="12:00",
    favorites=[
        Favorite("Slow", "1", "1", lead_minutes=60),
        Favorite("Normal", "2", "2", lead_minutes=30),
        Favorite("Fast", "3", "3", lead_minutes=15),
    ],
)

check(has_later_slot(cfg, datetime(2026, 7, 31, 11, 0)),
      "first slot offers Skip time slot")
check(has_later_slot(cfg, datetime(2026, 7, 31, 11, 30)),
      "middle slot offers Skip time slot")
check(not has_later_slot(cfg, datetime(2026, 7, 31, 11, 45)),
      "last slot offers only Skip today")


# The scheduled submit path must pass the selected pool index, not an undefined
# variable. Mock network/UI work so this remains a fast, side-effect-free test.
scheduled_cfg = Config(
    lunch_time="23:59",
    favorites=[Favorite("Only choice", "1", "1", lead_minutes=0)],
)
candidate = ("cart", [], [], 100, "PICKUP", None, "")
submitted = []
old_prepare = R.prepare_candidate
old_confirm = R.desktop_confirm
old_submit = R.submit_and_record
old_datetime = R.datetime
old_get_override = R.get_override
try:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 3, 23, 59)

    R.datetime = FixedDateTime
    R.prepare_candidate = lambda *_args: (candidate, "")
    R.desktop_confirm = lambda *_args, **_kwargs: "Place"
    R.submit_and_record = lambda *_args, **kwargs: submitted.append(kwargs)
    R.get_override = lambda *_args: None  # no override set — take the pool path
    R.run(scheduled_cfg, {"skip_dates": [], "orders": {}}, None, False)
finally:
    R.datetime = old_datetime
    R.prepare_candidate = old_prepare
    R.desktop_confirm = old_confirm
    R.submit_and_record = old_submit
    R.get_override = old_get_override
check(len(submitted) == 1 and submitted[0]["remember_cursor"],
      "scheduled orders submit without an undefined pool index")


# A state override for today forces that favorite via the single-pick path
# (remember_cursor=False, like an explicit --pick) instead of pool rotation —
# with no --pick on the CLI, just an "order tomorrow" choice made in advance.
override_cfg = Config(
    lunch_time="23:59",
    favorites=[Favorite("Overridden Pick", "1", "1", lead_minutes=0),
              Favorite("Other Fave", "2", "2", lead_minutes=0)],
)
overridden = []
old_prepare2 = R.prepare_candidate
old_confirm2 = R.desktop_confirm
old_submit2 = R.submit_and_record
old_datetime2 = R.datetime
old_get_override2 = R.get_override
try:
    class FixedDateTime2(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 3, 23, 59)

    R.datetime = FixedDateTime2
    R.prepare_candidate = lambda *_args: (candidate, "")
    R.desktop_confirm = lambda *_args, **_kwargs: "Place"
    R.submit_and_record = lambda *args, **kwargs: overridden.append((args, kwargs))
    R.get_override = lambda _iso: "Overridden Pick"
    R.run(override_cfg, {"skip_dates": [], "orders": {}}, None, False)
finally:
    R.datetime = old_datetime2
    R.prepare_candidate = old_prepare2
    R.desktop_confirm = old_confirm2
    R.submit_and_record = old_submit2
    R.get_override = old_get_override2
check(len(overridden) == 1 and overridden[0][0][2].store == "Overridden Pick"
      and overridden[0][1]["remember_cursor"] is False,
      "an override for today forces that favorite via the single-pick path")

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nall tests passed")
