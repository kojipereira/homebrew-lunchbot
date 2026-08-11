"""prepare_candidate resilience tests. Run with:
    PYTHONPATH=src python3.13 tests/test_order.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot import ddcli, order  # noqa: E402
from lunchbot.config import Config, Favorite  # noqa: E402


failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")
    else:
        print(f"ok:   {msg}")


fav = Favorite("Spot", "store-1", "order-1", lead_minutes=30)
cfg = Config(lunch_time="12:00", favorites=[fav], fulfillment="pickup")

old_dd = order.dd
old_cleanup = order.cleanup_carts_at_store
order.cleanup_carts_at_store = lambda *_a, **_k: None

# A transient dd-cli hiccup (e.g. a rate limit from repeated shuffling) during
# the reorder/preview call must not crash the whole run — prepare_candidate
# should treat it as "not preparable right now" (None, reason) rather than
# propagating the error.
try:
    def flaky_dd(*_args, **_kwargs):
        raise ddcli.DdError("dd-cli failed (1): rate limited")
    order.dd = flaky_dd
    try:
        candidate, _reason = order.prepare_candidate(cfg, fav)
        check(candidate is None, "a transient dd-cli error yields 'not preparable', not a crash")
    except ddcli.DdError:
        check(False, "a transient dd-cli error should not propagate out of prepare_candidate")
finally:
    order.dd = old_dd

# An expired sign-in, unlike a transient hiccup, must still surface so the
# app can offer the sign-in flow instead of silently skipping forever.
try:
    def logged_out_dd(*_args, **_kwargs):
        raise ddcli.NotLoggedIn("expired")
    order.dd = logged_out_dd
    try:
        order.prepare_candidate(cfg, fav)
        check(False, "NotLoggedIn should propagate out of prepare_candidate")
    except ddcli.NotLoggedIn:
        check(True, "NotLoggedIn still propagates out of prepare_candidate")
finally:
    order.dd = old_dd
    order.cleanup_carts_at_store = old_cleanup

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nall tests passed")
