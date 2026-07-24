"""Round-trip + diet-scan tests. Run with the resolved interpreter:
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
        diet="vegan", price_cap_cents=2500, work_benefits=True, lunch_time="12:00",
        weekdays=[1, 2, 4, 5],
        favorites=[
            # Apostrophe in the store name + a bareword-looking UUID: the two
            # values the plan calls out as breaking a naive TOML writer.
            C.Favorite(store="Joe's Diner", store_id="99999999",
                       reorder_from="00000000-0000-0000-0000-000000000000",
                       diet="vegan", lead_minutes=15),
            C.Favorite(store='Weird "Quoted" Cafe', store_id="1",
                       reorder_from="abc-123", diet="vegetarian", lead_minutes=60),
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
    check(back.diet == "vegan" and back.weekdays == [1, 2, 4, 5], "scalars round-trip")
    check(back.favorites[1].lead_minutes == 60, "lead_minutes round-trips")


def test_diet_scan():
    # Word-boundary: these are vegan-safe despite containing blocklist substrings.
    safe = [{"name": "Eggplant Parm-free Bowl"}, {"name": "Butternut Squash Soup"},
            {"name": "Veggie Hamburger-style Patty"}]
    check(C.diet_scan(safe, "vegan") == [], "word-boundary avoids eggplant/butternut false positives")
    # Real hits.
    check(C.diet_scan([{"name": "Grilled Chicken Wrap"}], "vegan"), "chicken flagged for vegan")
    check(C.diet_scan([{"name": "Cheese Pizza"}], "vegan"), "cheese flagged for vegan")
    check(C.diet_scan([{"name": "Cheese Pizza"}], "vegetarian") == [],
          "cheese allowed for vegetarian")
    check(C.diet_scan([{"name": "Tuna Melt"}], "vegetarian"), "fish flagged for vegetarian")
    check(C.diet_scan([{"name": "Grilled Chicken"}], "omnivore") == [], "omnivore scans nothing")


def test_eligibility():
    check(C.favorite_eligible("vegan", "vegetarian"), "vegan order ok for vegetarian")
    check(not C.favorite_eligible("vegetarian", "vegan"), "vegetarian order NOT ok for vegan")
    check(C.favorite_eligible("omnivore", "omnivore"), "omnivore ok for omnivore")


if __name__ == "__main__":
    test_roundtrip()
    test_diet_scan()
    test_eligibility()
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print("\nall tests passed")
