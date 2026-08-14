"""The daily job launchd fires: pick a favorite whose lead tier matches the
current time, walk it through preview/confirm/submit.

Two shapes, one per confirmation setting. With the confirm dialog on, each fire
offers whatever matches that tier and the user arbitrates (Place/Shuffle/Skip).
With it off — Yolo mode — nobody is at the keyboard, so the day's restaurant is
drawn up front from *all* the favorites and ordered when its own tier fires
(see daily_pick)."""

from __future__ import annotations

import logging
import random
from datetime import date, datetime

from . import ddcli
from .config import Config, Favorite
from .order import delete_cart, prepare_candidate, submit_and_record
from .state import (add_skip_date, already_ordered_today, get_daily_pick,
                    get_override, set_daily_pick)
from .ui import ask_retry_or_skip, desktop_confirm, notify

LEAD_TIME_TOLERANCE_MIN = 10


def current_lead_minutes(now: datetime, lunch_time: str) -> int:
    lh, lm = (int(x) for x in lunch_time.split(":"))
    lunch = now.replace(hour=lh, minute=lm, second=0, microsecond=0)
    return int((lunch - now).total_seconds() / 60)


def fires_now(fav: Favorite, lead_minutes_now: int | None) -> bool:
    """Whether this favorite's tier is the one firing at ``lead_minutes_now``."""
    return (lead_minutes_now is None
            or abs(fav.lead_minutes - lead_minutes_now) <= LEAD_TIME_TOLERANCE_MIN)


def get_pool(cfg: Config, lead_minutes_now: int | None) -> list[tuple[int, Favorite]]:
    return [(i, f) for i, f in enumerate(cfg.favorites)
            if fires_now(f, lead_minutes_now)]


def still_ahead(cfg: Config, lead_minutes_now: int) -> list[tuple[int, Favorite]]:
    """Favorites whose fire time is still to come today. A later tier means a
    *smaller* lead — 15 min before lunch happens after 60 min before lunch."""
    return [(i, f) for i, f in enumerate(cfg.favorites)
            if f.lead_minutes < lead_minutes_now - LEAD_TIME_TOLERANCE_MIN]


def reachable(cfg: Config, lead_minutes_now: int) -> list[tuple[int, Favorite]]:
    """Favorites that can still be ordered today: the tier firing right now plus
    every later one. A tier whose time has passed is not reachable — launchd
    won't fire it again until tomorrow."""
    return [(i, f) for i, f in enumerate(cfg.favorites)
            if f.lead_minutes <= lead_minutes_now + LEAD_TIME_TOLERANCE_MIN]


def slot_time(cfg: Config, fav: Favorite) -> str:
    """The HH:MM this favorite's tier fires at, for logs and notifications."""
    lh, lm = (int(x) for x in cfg.lunch_time.split(":"))
    t = (lh * 60 + lm - fav.lead_minutes) % (24 * 60)
    return f"{t // 60:02d}:{t % 60:02d}"


def has_later_slot(cfg: Config, now: datetime) -> bool:
    """Whether today's schedule has a firing time after ``now``."""
    lunch_hour, lunch_minute = (int(x) for x in cfg.lunch_time.split(":"))
    lunch_minutes = lunch_hour * 60 + lunch_minute
    current_minutes = now.hour * 60 + now.minute
    slot_minutes = {
        (lunch_minutes - favorite.lead_minutes) % (24 * 60)
        for favorite in cfg.favorites
    }
    return any(slot > current_minutes for slot in slot_minutes)


# ---- Yolo mode --------------------------------------------------------------
def daily_pick(cfg: Config, state: dict, today_iso: str, lead_now: int,
               persist: bool) -> tuple[int, Favorite] | None:
    """Yolo mode's restaurant for today — one favorite drawn at random from the
    *whole* list and remembered in state so every fire of the day agrees on it.
    None when no favorite's slot is still reachable.

    Drawing per-day rather than per-slot is the point. Yolo mode places an order
    the moment one previews cleanly, and `already_ordered_today` then closes the
    day — so picking within the tier that happens to fire first meant the
    earliest tier always won and a 15-minute favorite never got a turn behind a
    60-minute one. Choosing the restaurant first and waiting for *its* fire time
    gives every favorite the same odds, whatever its lead.
    """
    pool = reachable(cfg, lead_now)
    if not pool:
        return None
    remembered = get_daily_pick(state, today_iso)
    if remembered:
        for i, f in pool:
            if f.store == remembered:
                return i, f
        # Remembered pick is out of reach — its slot passed while the machine
        # was asleep, or prefs were saved mid-day and dropped it. Draw again.
        logging.info("today's pick %r is no longer reachable — re-picking", remembered)
    i, fav = random.choice(pool)
    if persist:
        set_daily_pick(state, today_iso, fav.store)
    logging.info("today's pick: %s (1 of %d reachable, fires %s)",
                 fav.store, len(pool), slot_time(cfg, fav))
    return i, fav


def run_yolo(cfg: Config, state: dict, today_iso: str, lead_now: int, dry: bool) -> None:
    """Order today's pick when its own tier fires.

    Yolo turns off the *confirmation* dialog, not the error one: nothing asks
    before a good order goes through, but a failure still gets the same
    Try again / Skip prompt the confirm path uses. If nobody answers it,
    `ask_retry_or_skip` times out to the least destructive option — Skip time
    slot when a later tier can still take the day, else Skip today.

    A dry run never pins the day's pick, so previewing the day can't commit the
    real fire to a restaurant.
    """
    picked = daily_pick(cfg, state, today_iso, lead_now, persist=not dry)
    if picked is None:
        logging.info("no favorite's fire time is still ahead (lead now %d min) — nothing to do",
                     lead_now)
        return
    pick_idx, fav = picked
    if not fires_now(fav, lead_now):
        logging.info("today's pick is %s at %s — not this slot, waiting for it",
                     fav.store, slot_time(cfg, fav))
        return

    while True:
        # The pick goes first; the rest of its own tier is the fallback,
        # shuffled, so one closed restaurant doesn't cost the day.
        fallbacks = [x for x in get_pool(cfg, lead_now) if x[0] != pick_idx]
        random.shuffle(fallbacks)
        order = [picked, *fallbacks]
        reason = "not available"
        for n, (idx, cand_fav) in enumerate(order, 1):
            logging.info("candidate [%d/%d]: %s", n, len(order), cand_fav.store)
            candidate, reason = prepare_candidate(cfg, cand_fav)
            if candidate is None:
                continue
            cart_uuid, _items, _line_items, total_cents, fulfillment, budget, team_id = candidate
            if dry:
                logging.info("DRY RUN — would submit %s for %d cents",
                             cand_fav.store, total_cents)
                delete_cart(cart_uuid)
                return
            if idx != pick_idx:
                # A fallback won. Record it as the day's pick so `lunchbot
                # status` names what's being ordered, not the one that was shut.
                set_daily_pick(state, today_iso, cand_fav.store)
            submit_and_record(cfg, state, cand_fav, cart_uuid, total_cents, fulfillment,
                              budget, team_id, idx, len(cfg.favorites), today_iso,
                              remember_cursor=True)
            return

        # Nothing in this tier worked — say so, and offer the same three ways
        # out the confirm path gives.
        logging.info("nothing orderable at %s (last: %s — %s)",
                     slot_time(cfg, fav), fav.store, reason)
        later = still_ahead(cfg, lead_now)
        next_slot = min(slot_time(cfg, f) for _i, f in later) if later else ""
        choice = ask_retry_or_skip(
            "Lunchbot: couldn't order",
            f"Nothing at your {slot_time(cfg, fav)} restaurants is orderable "
            f"(last: {fav.store} — {reason.rstrip('.')}). Try again"
            + (f", wait for {next_slot} and pick again, or skip today?"
               if later else ", or skip today?"),
            allow_skip_slot=bool(later))
        if choice == "Try again":
            continue
        if choice == "Skip today" or not later:
            add_skip_date(today_iso)
            notify("Lunchbot: skipped today", f"{fav.store} — {reason}")
            return
        # Skip time slot — hand the day to a later tier. This is also where an
        # unanswered dialog lands, which is the behaviour we want unattended.
        _, nxt = random.choice(later)
        if not dry:
            set_daily_pick(state, today_iso, nxt.store)
        logging.info("today's pick is now %s at %s", nxt.store, slot_time(cfg, nxt))
        notify("Lunchbot: trying later",
               f"{fav.store} — {reason}. Will try {nxt.store} at {slot_time(cfg, nxt)}.")
        return


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

    # An explicit --pick wins; otherwise a "order tomorrow" override set for
    # today takes the same single-favorite path instead of the pool rotation.
    # Gates above (weekday/skip/already-ordered) already ran either way — an
    # override changes *which* favorite fires, not whether today fires at all.
    pick = force_pick or get_override(today_iso)
    if pick:
        matches = [f for f in cfg.favorites if f.store.lower() == pick.lower()]
        if not matches:
            raise RuntimeError(f"no favorite named {pick!r}")
        fav = matches[0]
        candidate = None
        while candidate is None:
            candidate, reason = prepare_candidate(cfg, fav)
            if candidate is None:
                choice = ask_retry_or_skip(
                    "Lunchbot: couldn't order",
                    f"{fav.store} isn't orderable right now: "
                    f"{reason.rstrip('.')}. Try again?", allow_skip_slot=False)
                if choice == "Try again":
                    continue
                if choice == "Skip today":
                    add_skip_date(today_iso)
                    notify("Lunchbot: skipped today", f"{fav.store} — {reason}")
                    return
                raise RuntimeError(f"{fav.store} failed guards — see log")
        cart_uuid, items, line_items, total_cents, fulfillment, budget, team_id = candidate
        answer = (desktop_confirm(cfg, fav, items, line_items, total_cents, fulfillment,
                                   budget, allow_skip_slot=False, allow_shuffle=False,
                                   place_label=place_label)
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

    # Yolo mode decides across every tier, not just this one — see run_yolo.
    if not cfg.desktop_confirm.enabled:
        run_yolo(cfg, state, today_iso, lead_now, dry)
        return

    pool = get_pool(cfg, lead_now)
    if not pool:
        logging.info("no favorites match lead time %d min (asleep past a tier?) — nothing to do", lead_now)
        return
    # Randomize the order each fire so the pick isn't a predictable rotation.
    # The pool is already tier-filtered, so at the slow fire only slow-tier
    # favorites are here, at the fast fire only fast ones, etc. The user
    # arbitrates between tiers with Skip time slot.
    random.shuffle(pool)
    logging.info("pool (%d, shuffled): %s", len(pool), [f.store for _, f in pool])
    n = len(pool)
    idx = 0
    fail_streak = 0  # consecutive un-preparable candidates
    last_reason = "not available"

    while True:
        _, fav = pool[idx]
        logging.info("candidate [%d/%d]: %s", idx + 1, n, fav.store)
        candidate, last_reason = prepare_candidate(cfg, fav)
        if candidate is None:
            fail_streak += 1
            if fail_streak >= n:  # a full cycle, none preparable
                logging.info("no favorite is orderable right now")
                choice = ask_retry_or_skip(
                    "Lunchbot: couldn't order",
                    "None of your restaurants are orderable right now "
                    f"(last: {fav.store} — {last_reason.rstrip('.')}). Try "
                    "again, skip this time slot, or skip today?")
                if choice == "Try again":
                    fail_streak = 0
                    random.shuffle(pool)
                    idx = 0
                    continue
                if choice == "Skip today":
                    add_skip_date(today_iso)
                    notify("Lunchbot: skipped today", "No more orders will be offered today")
                    return
                notify("Lunchbot: skipped time slot", f"{fav.store} — {last_reason}")
                return
            idx = (idx + 1) % n   # Shuffle wraps: after the last, back to the first
            continue
        fail_streak = 0
        cart_uuid, items, line_items, total_cents, fulfillment, budget, team_id = candidate

        # Unconditional: Yolo mode never reaches here, it took run_yolo above.
        allow_skip_slot = has_later_slot(cfg, now)
        answer = desktop_confirm(cfg, fav, items, line_items, total_cents, fulfillment,
                                 budget, allow_skip_slot=allow_skip_slot, allow_shuffle=n > 1,
                                 place_label=place_label)

        if answer == "Shuffle":
            delete_cart(cart_uuid)
            idx = (idx + 1) % n
            continue
        if answer == "Skip today":
            delete_cart(cart_uuid)
            add_skip_date(today_iso)
            notify("Lunchbot: skipped today", "No more orders will be offered today")
            return
        if answer in ("Skip time slot", "TIMEOUT", "Skip"):
            delete_cart(cart_uuid)
            notify("Lunchbot: skipped time slot", f"{fav.store} — not approved")
            return

        if dry:
            logging.info("DRY RUN — would submit %s for %d cents", fav.store, total_cents)
            delete_cart(cart_uuid)
            return
        submit_and_record(cfg, state, fav, cart_uuid, total_cents, fulfillment, budget,
                          team_id, idx, len(pool), today_iso, remember_cursor=True)
        return
