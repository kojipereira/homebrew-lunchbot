"""The daily job launchd fires: pick a favorite whose lead tier matches the
current time, walk it through preview/confirm/submit."""

from __future__ import annotations

import logging
import random
from datetime import date, datetime

from . import ddcli
from .config import Config, Favorite
from .order import delete_cart, prepare_candidate, submit_and_record
from .state import already_ordered_today
from .ui import ask_retry, desktop_confirm, notify

LEAD_TIME_TOLERANCE_MIN = 10


def current_lead_minutes(now: datetime, lunch_time: str) -> int:
    lh, lm = (int(x) for x in lunch_time.split(":"))
    lunch = now.replace(hour=lh, minute=lm, second=0, microsecond=0)
    return int((lunch - now).total_seconds() / 60)


def get_pool(cfg: Config, lead_minutes_now: int | None) -> list[tuple[int, Favorite]]:
    out = []
    for i, f in enumerate(cfg.favorites):
        if lead_minutes_now is not None and abs(f.lead_minutes - lead_minutes_now) > LEAD_TIME_TOLERANCE_MIN:
            continue
        out.append((i, f))
    return out


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
        candidate = None
        while candidate is None:
            candidate = prepare_candidate(cfg, fav)
            if candidate is None:
                if ask_retry("Lunchbot: couldn't order",
                             f"Couldn't prepare {fav.store} right now (unavailable "
                             "or over your cap). Try again?"):
                    continue
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
    n = len(pool)
    idx = 0
    fail_streak = 0  # consecutive un-preparable candidates

    while True:
        _, fav = pool[idx]
        logging.info("candidate [%d/%d]: %s", idx + 1, n, fav.store)
        candidate = prepare_candidate(cfg, fav)
        if candidate is None:
            fail_streak += 1
            if fail_streak >= n:  # a full cycle, none preparable
                logging.info("no favorite is orderable right now")
                if ask_retry("Lunchbot: couldn't order",
                             "None of your restaurants are orderable right now "
                             "(unavailable or over your cap). Try again?"):
                    fail_streak = 0
                    random.shuffle(pool)
                    idx = 0
                    continue
                return
            idx = (idx + 1) % n   # Shuffle wraps: after the last, back to the first
            continue
        fail_streak = 0
        cart_uuid, items, line_items, total_cents, fulfillment, budget, team_id = candidate

        # Shuffle is always offered when there's more than one option; it cycles
        # through the pool and wraps around (3 options: 1→2→3→1→…).
        allow_next = n > 1
        answer = (desktop_confirm(cfg, fav, items, line_items, total_cents, fulfillment,
                                  budget, allow_next=allow_next, place_label=place_label)
                  if cfg.desktop_confirm.enabled else "Place")

        if answer == "Shuffle":
            delete_cart(cart_uuid)
            idx = (idx + 1) % n
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
