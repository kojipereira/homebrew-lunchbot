"""The daily job launchd fires: pick a diet-eligible favorite whose lead tier
matches the current time, walk it through preview/confirm/submit."""

from __future__ import annotations

import logging
import random
from datetime import date, datetime

from . import ddcli
from .config import Config, Favorite, favorite_eligible
from .order import delete_cart, prepare_candidate, submit_and_record
from .state import already_ordered_today
from .ui import desktop_confirm, notify, show_alert

LEAD_TIME_TOLERANCE_MIN = 10


def current_lead_minutes(now: datetime, lunch_time: str) -> int:
    lh, lm = (int(x) for x in lunch_time.split(":"))
    lunch = now.replace(hour=lh, minute=lm, second=0, microsecond=0)
    return int((lunch - now).total_seconds() / 60)


def get_pool(cfg: Config, lead_minutes_now: int | None) -> list[tuple[int, Favorite]]:
    out = []
    for i, f in enumerate(cfg.favorites):
        if not favorite_eligible(f.diet, cfg.diet):
            continue
        if lead_minutes_now is not None and abs(f.lead_minutes - lead_minutes_now) > LEAD_TIME_TOLERANCE_MIN:
            continue
        out.append((i, f))
    return out


def pick_from_pool(pool, cursor: int, tried: set[int]):
    n = len(pool)
    if n == 0:
        return None
    for offset in range(n):
        pool_idx = (cursor + offset) % n
        original_idx, fav = pool[pool_idx]
        if original_idx not in tried:
            return pool_idx, original_idx, fav
    return None


def run(cfg: Config, state: dict, force_pick: str | None, dry_run_override: bool) -> None:
    today = date.today()
    today_iso = today.isoformat()
    iso_weekday = today.weekday() + 1  # Python Mon=0..Sun=6 → 1..7 (Mon..Sun)

    if not force_pick:
        if iso_weekday not in cfg.weekdays:
            logging.info("weekday %d not in %s — skipping", iso_weekday, cfg.weekdays)
            return
        if today_iso in state.get("skip_dates", []):
            logging.info("in skip_dates — skipping")
            return
        if already_ordered_today(state):
            logging.info("already ordered today — skipping")
            return

    if cfg.delivery_address_id:
        try:
            ddcli.set_address(cfg.delivery_address_id)
            logging.info("confirmed delivery address: %s", cfg.delivery_address or cfg.delivery_address_id)
        except ddcli.DdError as e:
            logging.warning("could not set delivery address: %s", e)

    dry = dry_run_override or cfg.dry_run
    place_label = "OK (dry run)" if dry else "Place"
    now = datetime.now()
    lead_now = current_lead_minutes(now, cfg.lunch_time)
    logging.info("lead-to-lunch: %d min (lunch=%s, now=%s)",
                 lead_now, cfg.lunch_time, now.strftime("%H:%M"))

    if force_pick:
        matches = [f for f in cfg.favorites if f.store.lower() == force_pick.lower()]
        if not matches:
            raise RuntimeError(f"no favorite named {force_pick!r}")
        fav = matches[0]
        candidate = prepare_candidate(cfg, fav)
        if not candidate:
            raise RuntimeError(f"{fav.store} failed guards — see log")
        cart_uuid, items, line_items, total_cents, fulfillment, budget, team_id = candidate
        answer = (desktop_confirm(cfg, fav, items, line_items, total_cents, fulfillment,
                                  budget, allow_next=False, place_label=place_label)
                  if cfg.desktop_confirm.enabled else "Place")
        if answer != "Place":
            delete_cart(cart_uuid)
            notify("Lunchbot: skipped", f"{fav.store} — not approved")
            return
        if dry:
            logging.info("DRY RUN — would submit %s for %d cents", fav.store, total_cents)
            delete_cart(cart_uuid)
            return
        submit_and_record(cfg, state, fav, cart_uuid, total_cents, fulfillment, budget,
                          team_id, pool_idx=0, pool_len=0, today_iso=today_iso, remember_cursor=False)
        return

    pool = get_pool(cfg, lead_now)
    if not pool:
        logging.info("no favorites match lead time %d min (asleep past a tier?) — nothing to do", lead_now)
        return
    # Randomize the order each fire so the pick isn't a predictable rotation.
    # The pool is already tier-filtered, so at the slow fire only slow-tier
    # favorites are here, at the fast fire only fast ones, etc.
    random.shuffle(pool)
    logging.info("pool (%d, shuffled): %s", len(pool), [f.store for _, f in pool])
    tried: set[int] = set()

    while True:
        pick = pick_from_pool(pool, 0, tried)
        if pick is None:
            logging.info("exhausted all favorites")
            show_alert("Lunchbot", "Ran out of favorites — none passed guards / were approved.")
            return
        pool_idx, original_idx, fav = pick
        tried.add(original_idx)
        logging.info("candidate: %s (uuid=%s)", fav.store, fav.reorder_from)

        candidate = prepare_candidate(cfg, fav)
        if not candidate:
            continue
        cart_uuid, items, line_items, total_cents, fulfillment, budget, team_id = candidate

        allow_next = len(tried) < len(pool)
        answer = (desktop_confirm(cfg, fav, items, line_items, total_cents, fulfillment,
                                  budget, allow_next=allow_next, place_label=place_label)
                  if cfg.desktop_confirm.enabled else "Place")

        if answer == "Shuffle":
            delete_cart(cart_uuid)
            continue
        if answer in ("Skip", "TIMEOUT"):
            delete_cart(cart_uuid)
            notify("Lunchbot: skipped", f"{fav.store} — not approved")
            return

        if dry:
            logging.info("DRY RUN — would submit %s for %d cents", fav.store, total_cents)
            delete_cart(cart_uuid)
            return
        submit_and_record(cfg, state, fav, cart_uuid, total_cents, fulfillment, budget,
                          team_id, pool_idx, len(pool), today_iso, remember_cursor=True)
        return
