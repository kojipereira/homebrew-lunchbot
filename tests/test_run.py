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
try:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 3, 23, 59)

    R.datetime = FixedDateTime
    R.prepare_candidate = lambda *_args: candidate
    R.desktop_confirm = lambda *_args, **_kwargs: "Place"
    R.submit_and_record = lambda *_args, **kwargs: submitted.append(kwargs)
    R.run(scheduled_cfg, {"skip_dates": [], "orders": {}}, None, False)
finally:
    R.datetime = old_datetime
    R.prepare_candidate = old_prepare
    R.desktop_confirm = old_confirm
    R.submit_and_record = old_submit
check(len(submitted) == 1 and submitted[0]["remember_cursor"],
      "scheduled orders submit without an undefined pool index")

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nall tests passed")
