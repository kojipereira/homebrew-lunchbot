"""Config round-trip tests. Run with the resolved interpreter:
    PYTHONPATH=src python3.13 tests/test_config.py
Exits non-zero on failure. No test framework (stdlib only)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot import config as C  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")
    else:
        print(f"ok:   {msg}")


def test_roundtrip():
    cfg = C.Config(
        fulfillment="either", price_cap_cents=2500, work_benefits=True,
        lunch_time="12:00", weekdays=[1, 2, 4, 5], lead_tiers={"fast": 10, "normal": 25, "slow": 45},
        favorites=[
            # Apostrophe in the store name + a bareword-looking UUID: the two
            # values the plan calls out as breaking a naive TOML writer.
            C.Favorite(store="Joe's Diner", store_id="99999999",
                       reorder_from="00000000-0000-0000-0000-000000000000",
                       lead_minutes=15),
            C.Favorite(store='Weird "Quoted" Cafe', store_id="1",
                       reorder_from="abc-123", lead_minutes=60),
        ],
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.toml"
        C.write_config(cfg, p)
        back = C.load_config(p)
    check(back.favorites[0].store == "Joe's Diner", "apostrophe store round-trips")
    check(back.favorites[0].reorder_from == "00000000-0000-0000-0000-000000000000",
          "UUID round-trips as string")
    check(back.favorites[1].store == 'Weird "Quoted" Cafe', "embedded quotes round-trip")
    check(back.weekdays == [1, 2, 4, 5], "scalars round-trip")
    check(back.favorites[1].lead_minutes == 60, "lead_minutes round-trips")
    check(back.fulfillment == "either", "fulfillment=either round-trips")
    check(back.lead_tiers == {"fast": 10, "normal": 25, "slow": 45}, "lead_tiers round-trip")


def test_legacy_diet_keys_ignored():
    """Configs written before diet was dropped must still load — the stale
    `diet` keys are simply ignored, not rejected."""
    legacy = """
diet = "vegan"
fulfillment = "pickup"
lunch_time = "12:00"

[[favorites]]
store = "Old Favorite"
store_id = "1"
reorder_from = "abc-123"
diet = "vegetarian"
lead_minutes = 45
"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.toml"
        p.write_text(legacy)
        cfg = C.load_config(p)
    check(cfg.favorites[0].store == "Old Favorite", "legacy config with diet keys loads")
    check(cfg.favorites[0].lead_minutes == 45, "legacy lead_minutes survives")
    check(not hasattr(cfg, "diet"), "Config no longer carries a diet field")


if __name__ == "__main__":
    test_roundtrip()
    test_legacy_diet_keys_ignored()
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print("\nall tests passed")
