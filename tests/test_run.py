"""Scheduled-slot behavior tests. Run with:
    PYTHONPATH=src python3.13 tests/test_run.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot.config import Config, DesktopConfirmCfg, Favorite  # noqa: E402
from lunchbot import run as R  # noqa: E402
from lunchbot.run import daily_pick, has_later_slot, reachable, still_ahead  # noqa: E402


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


# When only one favorite matches the current time slot, the dialog must not
# offer Shuffle at all — there's nothing to shuffle to.
single_cfg = Config(
    lunch_time="23:59",
    favorites=[Favorite("Only choice", "1", "1", lead_minutes=0)],
)
seen_allow_shuffle = []
old_prepare = R.prepare_candidate
old_confirm = R.desktop_confirm
old_submit = R.submit_and_record
old_datetime = R.datetime
try:
    class FixedDateTime3(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 3, 23, 59)

    def fake_confirm(*_args, **kwargs):
        seen_allow_shuffle.append(kwargs.get("allow_shuffle"))
        return "Place"

    R.datetime = FixedDateTime3
    R.prepare_candidate = lambda *_args: (candidate, "")
    R.desktop_confirm = fake_confirm
    R.submit_and_record = lambda *_args, **_kwargs: None
    R.run(single_cfg, {"skip_dates": [], "orders": {}}, None, False)
finally:
    R.datetime = old_datetime
    R.prepare_candidate = old_prepare
    R.desktop_confirm = old_confirm
    R.submit_and_record = old_submit
check(seen_allow_shuffle == [False],
      "a single-restaurant time slot never offers Shuffle")


# ---- Yolo mode --------------------------------------------------------------
# Lunch 12:00 with three tiers: Slow fires 11:00, Normal 11:30, Fast 11:45.
yolo_cfg = Config(
    lunch_time="12:00",
    desktop_confirm=DesktopConfirmCfg(enabled=False),
    favorites=[
        Favorite("Slow", "1", "1", lead_minutes=60),
        Favorite("Normal", "2", "2", lead_minutes=30),
        Favorite("Fast", "3", "3", lead_minutes=15),
    ],
)

# At the 11:00 fire (lead 60) every tier is still ahead, so the draw spans all
# three — the bug was that only Slow could ever win.
check(sorted(f.store for _i, f in reachable(yolo_cfg, 60)) == ["Fast", "Normal", "Slow"],
      "the first fire can still reach every tier")
check(sorted(f.store for _i, f in reachable(yolo_cfg, 30)) == ["Fast", "Normal"],
      "a tier whose time has passed is out of reach")
check(sorted(f.store for _i, f in still_ahead(yolo_cfg, 60)) == ["Fast", "Normal"],
      "still_ahead excludes the tier firing right now")
check(still_ahead(yolo_cfg, 15) == [], "the last tier has nothing after it")


def run_yolo_at(cfg, lead_now, state=None, prepare=None, dry=False, answers=None):
    """Drive run_yolo with the network and state writes stubbed out. `answers`
    is the queue of error-dialog replies; the last one repeats. Returns
    (submitted_store_names, state, dialogs_shown)."""
    submitted, dialogs = [], []
    replies = list(answers or ["Skip time slot"])
    state = {"skip_dates": [], "orders": {}} if state is None else state
    old = (R.prepare_candidate, R.submit_and_record, R.set_daily_pick, R.notify,
           R.delete_cart, R.ask_retry_or_skip, R.add_skip_date)

    def fake_dialog(_title, body, **kwargs):
        dialogs.append((body, kwargs.get("allow_skip_slot")))
        return replies.pop(0) if len(replies) > 1 else replies[0]

    try:
        R.prepare_candidate = prepare or (lambda _cfg, _fav: (candidate, ""))
        R.submit_and_record = lambda _c, _s, fav, *_a, **_k: submitted.append(fav.store)
        # The real versions write the developer's own state file and shell out
        # to dd-cli.
        R.set_daily_pick = lambda st, iso, store: st.update(
            {"daily_pick": {"date": iso, "store": store}})
        R.add_skip_date = lambda iso: state.setdefault("skip_dates", []).append(iso)
        R.delete_cart = lambda *_a: None
        R.notify = lambda *_a: None
        R.ask_retry_or_skip = fake_dialog
        R.run_yolo(cfg, state, "2026-08-14", lead_now, dry)
    finally:
        (R.prepare_candidate, R.submit_and_record, R.set_daily_pick, R.notify,
         R.delete_cart, R.ask_retry_or_skip, R.add_skip_date) = old
    return submitted, state, dialogs


# Every tier must be able to win the day. Draw repeatedly at the first fire and
# check the pick isn't pinned to the earliest tier.
picks = set()
for _ in range(200):
    st = {"skip_dates": [], "orders": {}}
    R_set = R.set_daily_pick
    try:
        R.set_daily_pick = lambda s, iso, store: s.update(
            {"daily_pick": {"date": iso, "store": store}})
        got = daily_pick(yolo_cfg, st, "2026-08-14", 60, persist=True)
    finally:
        R.set_daily_pick = R_set
    picks.add(got[1].store)
check(picks == {"Slow", "Normal", "Fast"},
      "Yolo draws the day's restaurant from every tier, not just the first")

# ...and once drawn, the pick is sticky: later fires of the same day agree.
sticky = {"skip_dates": [], "orders": {},
          "daily_pick": {"date": "2026-08-14", "store": "Fast"}}
check(all(daily_pick(yolo_cfg, sticky, "2026-08-14", 60, persist=False)[1].store == "Fast"
          for _ in range(20)),
      "a pick already drawn for today is reused, not re-rolled")

# Yesterday's pick is not today's: a stale entry is redrawn and restamped.
stale = {"skip_dates": [], "orders": {},
         "daily_pick": {"date": "2026-08-13", "store": "Fast"}}
R_set = R.set_daily_pick
try:
    R.set_daily_pick = lambda s, iso, store: s.update(
        {"daily_pick": {"date": iso, "store": store}})
    _pick = daily_pick(yolo_cfg, stale, "2026-08-14", 60, persist=True)
finally:
    R.set_daily_pick = R_set
check(stale["daily_pick"]["date"] == "2026-08-14"
      and stale["daily_pick"]["store"] == _pick[1].store,
      "a pick stamped with an earlier date is redrawn for today")

# The heart of it: a fire that isn't the pick's own slot must order nothing.
submitted, _st, dialogs = run_yolo_at(
    yolo_cfg, 60, state={"skip_dates": [], "orders": {},
                         "daily_pick": {"date": "2026-08-14", "store": "Fast"}})
check(submitted == [] and dialogs == [],
      "the 11:00 fire does not order when the day's pick is the 11:45 one")

# ...and that same pick does order once its own slot comes around, silently:
# Yolo drops the *confirmation* dialog, so a good order asks nothing.
submitted, _st, dialogs = run_yolo_at(
    yolo_cfg, 15, state={"skip_dates": [], "orders": {},
                         "daily_pick": {"date": "2026-08-14", "store": "Fast"}})
check(submitted == ["Fast"] and dialogs == [],
      "the day's pick orders at its own time slot, with no dialog")

# A pick that isn't orderable falls back within its own tier rather than losing
# the day. Two favorites share the 11:45 slot; the picked one is closed.
tie_cfg = Config(
    lunch_time="12:00",
    desktop_confirm=DesktopConfirmCfg(enabled=False),
    favorites=[Favorite("Closed", "1", "1", lead_minutes=15),
               Favorite("Open", "2", "2", lead_minutes=15)],
)
submitted, _st, dialogs = run_yolo_at(
    tie_cfg, 15,
    state={"skip_dates": [], "orders": {},
           "daily_pick": {"date": "2026-08-14", "store": "Closed"}},
    prepare=lambda _cfg, fav: ((None, "closed") if fav.store == "Closed"
                               else (candidate, "")))
check(submitted == ["Open"] and dialogs == [],
      "an unorderable pick falls back within its own slot without bothering anyone")

# Errors are the exception: when the whole tier is unorderable, Yolo mode still
# raises the dialog rather than failing quietly.
all_closed = lambda _cfg, _fav: (None, "closed")  # noqa: E731
submitted, st, dialogs = run_yolo_at(
    yolo_cfg, 60,
    state={"skip_dates": [], "orders": {},
           "daily_pick": {"date": "2026-08-14", "store": "Slow"}},
    prepare=all_closed)
check(len(dialogs) == 1, "Yolo mode still shows an error dialog when nothing is orderable")
check("closed" in dialogs[0][0] and "Slow" in dialogs[0][0],
      "the error dialog names the restaurant and DoorDash's reason")
check(dialogs[0][1] is True, "...and offers Skip time slot while a later tier remains")
# The default answer (also where an unanswered dialog times out) hands the day
# to a later tier: no order now, stored pick moves forward.
check(submitted == [] and st["daily_pick"]["store"] in ("Normal", "Fast"),
      "Skip time slot re-draws today's pick from the tiers still ahead")

# Try again re-runs the slot; the second pass succeeds and orders.
attempts = []


def flaky(_cfg, fav):
    attempts.append(fav.store)
    return (None, "closed") if len(attempts) == 1 else (candidate, "")


submitted, _st, dialogs = run_yolo_at(
    yolo_cfg, 15,
    state={"skip_dates": [], "orders": {},
           "daily_pick": {"date": "2026-08-14", "store": "Fast"}},
    prepare=flaky, answers=["Try again"])
check(submitted == ["Fast"] and len(dialogs) == 1,
      "Try again re-attempts the slot instead of giving up")

# On the last tier there's nothing to defer to, so only Skip today is offered.
submitted, st, dialogs = run_yolo_at(
    yolo_cfg, 15,
    state={"skip_dates": [], "orders": {},
           "daily_pick": {"date": "2026-08-14", "store": "Fast"}},
    prepare=all_closed, answers=["Skip today"])
check(dialogs[0][1] is False and st["skip_dates"] == ["2026-08-14"],
      "the last tier offers only Skip today, and taking it skips the day")

# A dry run must not leave a pick behind for the real fire to honour.
_submitted, st, _dialogs = run_yolo_at(yolo_cfg, 60, dry=True)
check("daily_pick" not in st, "a dry run previews the day without pinning its pick")

# Yolo must not reach the *confirmation* dialog — that's the one it turns off.
prompted = []
old_confirm4 = R.desktop_confirm
old_retry4 = R.ask_retry_or_skip
old_datetime4 = R.datetime
old_override4 = R.get_override
old_prepare4 = R.prepare_candidate
old_submit4 = R.submit_and_record
old_setpick4 = R.set_daily_pick
try:
    class FixedDateTime4(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 14, 11, 45)   # the 15-min tier's fire

    R.datetime = FixedDateTime4
    R.get_override = lambda *_a: None
    R.desktop_confirm = lambda *a, **k: prompted.append("confirm") or "Place"
    R.ask_retry_or_skip = lambda *a, **k: prompted.append("retry") or "Skip today"
    R.prepare_candidate = lambda _cfg, _fav: (candidate, "")
    R.submit_and_record = lambda *_a, **_k: None
    R.set_daily_pick = lambda s, iso, store: s.update(
        {"daily_pick": {"date": iso, "store": store}})
    R.run(Config(lunch_time="12:00",
                 desktop_confirm=DesktopConfirmCfg(enabled=False),
                 weekdays=[1, 2, 3, 4, 5, 6, 7],
                 favorites=[Favorite("Only", "1", "1", lead_minutes=15)]),
          {"skip_dates": [], "orders": {}}, None, False)
finally:
    R.datetime = old_datetime4
    R.get_override = old_override4
    R.desktop_confirm = old_confirm4
    R.ask_retry_or_skip = old_retry4
    R.prepare_candidate = old_prepare4
    R.submit_and_record = old_submit4
    R.set_daily_pick = old_setpick4
check(prompted == [], "a clean Yolo order asks nothing before placing")


if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nall tests passed")
