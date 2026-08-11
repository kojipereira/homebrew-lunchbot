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

# ---- extract_fulfillment ----------------------------------------------------
# DoorDash omits `fulfillment_type` on delivery carts (DELIVERY is the proto
# default, so it never serializes). Reading only that field made every delivery
# attempt read as "unavailable (got none)" and silently reduced "either" to
# pickup-only. These payloads mirror real `order preview` responses.
def _preview(**cart) -> dict:
    return {"quote": {"store_order_cart": cart}}


# The shape that broke delivery: no fulfillment_type, is_consumer_pickup False.
check(order.extract_fulfillment(_preview(is_consumer_pickup=False)) == "DELIVERY",
      "a delivery cart (fulfillment_type absent) reads as DELIVERY")
check(order.extract_fulfillment(_preview(is_consumer_pickup=True)) == "PICKUP",
      "a pickup cart with only is_consumer_pickup reads as PICKUP")
# When DoorDash does send the field, it still wins (unchanged pickup path).
check(order.extract_fulfillment(
          _preview(fulfillment_type="PICKUP", is_consumer_pickup=True)) == "PICKUP",
      "an explicit fulfillment_type=PICKUP still reads as PICKUP")
check(order.extract_fulfillment(
          _preview(fulfillment_type="DELIVERY", is_consumer_pickup=False)) == "DELIVERY",
      "an explicit fulfillment_type=DELIVERY still reads as DELIVERY")
# Genuinely unreadable payloads must stay "" so the caller skips the mode
# rather than guessing a mode DoorDash never confirmed.
check(order.extract_fulfillment({}) == "", "an empty payload reads as unknown")
check(order.extract_fulfillment({"quote": None}) == "", "a null quote reads as unknown")
check(order.extract_fulfillment({"quote": {"store_order_cart": None}}) == "",
      "a null store_order_cart reads as unknown")
check(order.extract_fulfillment(_preview(menu={})) == "",
      "a cart carrying neither mode field reads as unknown")

# ---- delivery end-to-end through prepare_candidate --------------------------
# The regression that matters: with fulfillment="delivery" and a realistic
# delivery preview, prepare_candidate must return a candidate rather than
# rejecting the mode.
order.cleanup_carts_at_store = lambda *_a, **_k: None
try:
    deleted: list[str] = []

    def delivery_dd(*args, **_kwargs):
        if args[:2] == ("order", "reorder"):
            return {"success": True, "cart_uuid": "cart-1"}
        if args[:2] == ("order", "preview"):
            check("--fulfillment" in args and args[args.index("--fulfillment") + 1] == "delivery",
                  "the preview is asked for delivery explicitly")
            return {
                "quote": {
                    # No fulfillment_type — exactly what DoorDash returns here.
                    "store_order_cart": {"is_consumer_pickup": False,
                                         "orders": [{"order_items": [{"item": {"name": "Bowl"}}]}]},
                    "net_total_before_tip": {"unit_amount": 1850},
                    "line_items": [{"charge_id": "DELIVERY_FEE", "label": "Delivery Fee",
                                    "final_money": {"display_string": "$0.00"}}],
                    "delivery_availability": {"asap_available": True,
                                              "is_within_delivery_region": True},
                },
            }
        if args[:2] == ("cart", "delete"):
            deleted.append(args[3])
            return {}
        raise AssertionError(f"unexpected dd call: {args}")

    order.dd = delivery_dd
    dcfg = Config(lunch_time="12:00", favorites=[fav], fulfillment="delivery",
                  price_cap_cents=2500)
    cand, _reason = order.prepare_candidate(dcfg, fav)
    check(cand is not None, "a delivery-only config prepares a candidate")
    if cand:
        check(cand[4] == "DELIVERY", f"the candidate carries DELIVERY (got {cand[4]!r})")
        check(cand[3] == 1850, f"the candidate carries the delivery total (got {cand[3]!r})")
    check(deleted == [], "a passing delivery candidate keeps its cart")

    # A delivery cart over the cap is still rejected, and still cleans up.
    order.dd = delivery_dd
    capped, cap_reason = order.prepare_candidate(
        Config(lunch_time="12:00", favorites=[fav], fulfillment="delivery",
               price_cap_cents=1000), fav)
    check(capped is None, "a delivery candidate over the price cap is rejected")
    check("cap" in cap_reason, f"the rejection reason mentions the cap (got {cap_reason!r})")
    check(deleted == ["cart-1"], "a rejected delivery candidate deletes its cart")
finally:
    order.dd = old_dd
    order.cleanup_carts_at_store = old_cleanup

# ---- pickup distance limit --------------------------------------------------
# Real coordinates from a live preview: the office on New Montgomery and Matko
# on Valencia, 2.15 mi apart. DoorDash reports offers_pickup=True for it.
OFFICE = (37.787205, -122.400323)
MATKO = (37.76112, -122.421676)
MIXT = (37.788500, -122.399700)   # 560 Mission St — around the corner


def _preview_at(store_ll, *, offers_pickup=True, total=1500) -> dict:
    return {"quote": {
        "store_order_cart": {
            "is_consumer_pickup": True,
            "store": {"offers_pickup": offers_pickup,
                      "address": {"lat": store_ll[0], "lng": store_ll[1]}},
            "orders": [{"order_items": [{"item": {"name": "Bowl"}}]}],
        },
        "delivery_address": {"lat": OFFICE[0], "lng": OFFICE[1]},
        "net_total_before_tip": {"unit_amount": total},
        "line_items": [],
    }}


far = order.store_distance_miles(_preview_at(MATKO))
near = order.store_distance_miles(_preview_at(MIXT))
check(far is not None and 2.0 < far < 2.3, f"Matko measures ~2.15 mi (got {far})")
check(near is not None and near < 0.2, f"MIXT measures a short walk (got {near})")
check(order.store_distance_miles({}) is None, "a payload without coordinates yields no distance")
check(order.store_distance_miles(
          {"quote": {"delivery_address": {"lat": 1.0, "lng": 2.0}}}) is None,
      "a payload missing the store coordinate yields no distance")


def _run_modes(cfg_fulfillment, store_ll, max_miles=1.0):
    """prepare_candidate against one store; returns the modes actually previewed."""
    tried: list[str] = []

    def dd_stub(*args, **_kwargs):
        if args[:2] == ("order", "reorder"):
            return {"success": True, "cart_uuid": "cart-x"}
        if args[:2] == ("order", "preview"):
            mode = args[args.index("--fulfillment") + 1]
            tried.append(mode)
            p = _preview_at(store_ll)
            # Mirror DoorDash: fulfillment_type present only for pickup, and
            # absent (i.e. DELIVERY) otherwise.
            p["quote"]["store_order_cart"]["is_consumer_pickup"] = (mode == "pickup")
            return p
        if args[:2] == ("cart", "delete"):
            return {}
        raise AssertionError(f"unexpected dd call: {args}")

    order.dd = dd_stub
    cfg_x = Config(lunch_time="12:00", favorites=[fav], fulfillment=cfg_fulfillment,
                   max_pickup_miles=max_miles, price_cap_cents=2500)
    cand, _reason = order.prepare_candidate(cfg_x, fav)
    return tried, cand


order.cleanup_carts_at_store = lambda *_a, **_k: None
try:
    # "either" at a far store: pickup is previewed, rejected on distance, and
    # delivery takes it. This is the behavior the whole change exists for.
    tried, cand = _run_modes("either", MATKO)
    check(tried == ["pickup", "delivery"], f"'either' falls through to delivery when far (got {tried})")
    check(cand is not None and cand[4] == "DELIVERY",
          f"a far store resolves to DELIVERY (got {cand and cand[4]})")

    # "either" around the corner: pickup wins, delivery never attempted.
    tried, cand = _run_modes("either", MIXT)
    check(tried == ["pickup"], f"'either' keeps pickup when close (got {tried})")
    check(cand is not None and cand[4] == "PICKUP",
          f"a nearby store resolves to PICKUP (got {cand and cand[4]})")

    # An explicit choice is an instruction, not a hint: the distance limit must
    # not override it in either direction.
    tried, cand = _run_modes("pickup", MATKO)
    check(tried == ["pickup"] and cand is not None and cand[4] == "PICKUP",
          f"explicit 'pickup' is honored even 2 mi out (got {tried}, {cand and cand[4]})")
    tried, cand = _run_modes("delivery", MIXT)
    check(tried == ["delivery"] and cand is not None and cand[4] == "DELIVERY",
          f"explicit 'delivery' is honored next door (got {tried}, {cand and cand[4]})")

    # A raised limit brings pickup back for the same far store.
    tried, cand = _run_modes("either", MATKO, max_miles=5.0)
    check(tried == ["pickup"] and cand is not None and cand[4] == "PICKUP",
          f"raising max_pickup_miles restores pickup (got {tried}, {cand and cand[4]})")
finally:
    order.dd = old_dd
    order.cleanup_carts_at_store = old_cleanup

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nall tests passed")
