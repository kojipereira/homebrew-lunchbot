"""Reorder → preview → guard → submit → verify. The load-bearing logic,
ported from the original single-file script (behavior preserved), with the
vegan boolean generalized to the diet model and config.yaml→config.toml.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from . import config as cfgmod
from .config import Config, Favorite
from .ddcli import dd
from .state import save_state
from .ui import show_alert


# ---- preview extractors -----------------------------------------------------
def extract_preview_items(preview: dict) -> list[dict]:
    try:
        return preview["quote"]["store_order_cart"]["orders"][0]["order_items"]
    except (KeyError, IndexError, TypeError):
        return []


def extract_total_cents(preview: dict) -> int:
    try:
        return int(preview["quote"]["net_total_before_tip"]["unit_amount"])
    except (KeyError, TypeError):
        return -1


def extract_fulfillment(preview: dict) -> str:
    try:
        return preview["quote"]["store_order_cart"]["fulfillment_type"].upper()
    except (KeyError, TypeError):
        return ""


def extract_line_items(preview: dict) -> list[tuple[str, str]]:
    try:
        rows = preview["quote"]["line_items"]
    except (KeyError, TypeError):
        return []
    out = []
    for r in rows:
        label = r.get("label") or r.get("charge_id") or "?"
        disp = ((r.get("final_money") or {}).get("display_string")) or ""
        if disp:
            out.append((label, disp))
    return out


def pick_budget(preview: dict) -> tuple[dict, str] | None:
    """Return (largest eligible budget entry, team_id) or None."""
    try:
        budgets = preview["quote"]["expense_order_options"]["all_eligible_expense_order_budgets"] or []
    except (KeyError, TypeError):
        return None
    eligible = [b for b in budgets
                if ((b.get("remaining_amount") or {}).get("unit_amount", 0)) > 0]
    if not eligible:
        return None
    picked = max(eligible, key=lambda b: b["remaining_amount"]["unit_amount"])
    try:
        team_id = preview["quote"]["company_payment_info"]["team_order_info"]["team_id"]
    except (KeyError, TypeError):
        team_id = ""
    return (picked, team_id) if team_id else None


# ---- cart hygiene -----------------------------------------------------------
def cleanup_carts_at_store(store_id: str) -> None:
    """Delete pre-existing open carts at a store so a stale cart can't inflate
    the reorder (this is what caused the $37 Papa Noodle preview)."""
    try:
        listing = dd("cart", "list", "--store-id", store_id)
    except Exception as e:
        logging.warning("cart list failed for store %s: %s", store_id, e)
        return
    for c in listing.get("carts", []) or []:
        cart_uuid = c.get("cart_uuid")
        if not cart_uuid:
            continue
        try:
            dd("cart", "delete", "--cart-uuid", cart_uuid)
            logging.info("deleted orphan cart %s at store %s", cart_uuid, store_id)
        except Exception as e:
            logging.warning("failed to delete orphan cart %s: %s", cart_uuid, e)


def delete_cart(cart_uuid: str) -> None:
    try:
        dd("cart", "delete", "--cart-uuid", cart_uuid)
        logging.info("deleted cart %s", cart_uuid)
    except Exception as e:
        logging.warning("failed to delete cart %s: %s", cart_uuid, e)


# ---- prepare + submit -------------------------------------------------------
Candidate = tuple  # (cart_uuid, items, line_items, total_cents, fulfillment, budget, team_id)


def prepare_candidate(cfg: Config, fav: Favorite) -> Candidate | None:
    """Cleanup orphans → reorder → preview (force pickup + work benefits) →
    guards. Returns the candidate tuple or None; always deletes its own cart on
    a guard failure so we never leak carts."""
    cleanup_carts_at_store(fav.store_id)

    reorder = dd("order", "reorder", "--order-uuid", fav.reorder_from)
    if not reorder.get("success", False):
        logging.warning("reorder failed for %s: %s", fav.store, reorder.get("fail_reason"))
        return None
    cart_uuid = reorder["cart_uuid"]

    preview_args = ["order", "preview", "--cart-uuid", cart_uuid, "--fulfillment", cfg.fulfillment]
    if cfg.work_benefits:
        preview_args.append("--include-work-benefits")
    preview = dd(*preview_args)

    fulfillment = extract_fulfillment(preview)
    expected = cfg.fulfillment.upper()
    if fulfillment != expected:
        logging.warning("%s: fulfillment mismatch (wanted=%s, got=%s) — skipping",
                        fav.store, expected, fulfillment)
        delete_cart(cart_uuid)
        return None

    budget: dict | None = None
    team_id = ""
    if cfg.work_benefits:
        picked = pick_budget(preview)
        if picked is None:
            logging.warning("%s: no eligible work-benefit budget — skipping", fav.store)
            delete_cart(cart_uuid)
            return None
        budget, team_id = picked
        preview = dd("order", "preview", "--cart-uuid", cart_uuid,
                     "--selected-budget-id", budget["id"])

    items = extract_preview_items(preview)
    line_items = extract_line_items(preview)
    total_cents = extract_total_cents(preview)
    fulfillment = extract_fulfillment(preview)
    logging.info("preview: %s — %d items, $%.2f, %s%s", fav.store, len(items),
                 total_cents / 100 if total_cents >= 0 else -1, fulfillment,
                 f", budget={budget['name']!r}" if budget else "")
    for lbl, disp in line_items:
        logging.info("  %s: %s", lbl, disp)

    if total_cents < 0:
        logging.warning("could not read total for %s", fav.store)
        delete_cart(cart_uuid)
        return None
    if total_cents > cfg.price_cap_cents:
        logging.warning("%s over cap: %d > %d", fav.store, total_cents, cfg.price_cap_cents)
        delete_cart(cart_uuid)
        return None
    hits = cfgmod.diet_scan(items, cfg.diet)
    if hits:
        logging.warning("%s failed %s scan: %s", fav.store, cfg.diet, "; ".join(hits))
        delete_cart(cart_uuid)
        return None
    return cart_uuid, items, line_items, total_cents, fulfillment, budget, team_id


def submit_and_record(cfg: Config, state: dict, fav: Favorite, cart_uuid: str,
                      total_cents: int, fulfillment: str, budget: dict | None,
                      team_id: str, pool_idx: int, pool_len: int, today_iso: str,
                      remember_cursor: bool) -> None:
    tip = 0 if fulfillment == "PICKUP" else cfg.default_tip_cents
    submit_args = ["order", "submit", "--cart-uuid", cart_uuid,
                   "--fulfillment", cfg.fulfillment, "--tip-cents", str(tip), "--yes"]
    if cfg.work_benefits:
        if not (budget and team_id):
            show_alert("Lunchbot: submit blocked",
                       f"{fav.store}: work_benefits=true but no budget resolved.")
            delete_cart(cart_uuid)
            return
        code_mode = budget.get("expense_code_mode", "NONE")
        note_required = bool(budget.get("is_expense_note_required"))
        if code_mode == "REQUIRED" and not cfg.default_expense_code:
            show_alert("Lunchbot: submit blocked",
                       f"Budget {budget.get('name','?')!r} requires an expense code — "
                       "set default_expense_code in config.toml (`lunchbot setup`).")
            delete_cart(cart_uuid)
            return
        if note_required and not cfg.default_expense_note:
            show_alert("Lunchbot: submit blocked",
                       f"Budget {budget.get('name','?')!r} requires an expense note — "
                       "set default_expense_note in config.toml (`lunchbot setup`).")
            delete_cart(cart_uuid)
            return
        submit_args += ["--team-id", team_id, "--budget-id", budget["id"]]
        if budget.get("team_account_id"):
            submit_args += ["--team-account-id", budget["team_account_id"]]
        if cfg.default_expense_code and code_mode != "NONE":
            submit_args += ["--expense-code", cfg.default_expense_code]
        if cfg.default_expense_note and note_required:
            submit_args += ["--expense-notes", cfg.default_expense_note]

    submit = dd(*submit_args)
    order_uuid = submit.get("order_uuid") or ""
    if not submit.get("success", False) or not order_uuid:
        err = submit.get("error_message") or submit.get("message") or "unknown error"
        logging.error("submit failed for %s: %s", fav.store, err)
        checkout_url = ""
        try:
            url_resp = dd("order", "checkout-url", "--cart-uuid", cart_uuid)
            checkout_url = url_resp.get("checkout_url") or url_resp.get("url") or ""
        except Exception as e:
            logging.warning("could not get checkout URL: %s", e)
        show_alert("Lunchbot: submit FAILED",
                   f"{fav.store} — ${total_cents/100:.2f}\n\nDoorDash rejected the order.\n{err}\n\n"
                   + (f"Finish in browser:\n{checkout_url}" if checkout_url
                      else "Try again in the DoorDash app."))
        return
    logging.info("submitted: %s", order_uuid)

    state.setdefault("orders", {})[today_iso] = {
        "store": fav.store, "order_uuid": order_uuid, "cart_uuid": cart_uuid,
        "total_cents": total_cents, "submitted_at": datetime.now().isoformat(),
    }
    if remember_cursor and pool_len > 0:
        state["cursor"] = (pool_idx + 1) % pool_len
    save_state(state)

    final_status = "pending"
    status: dict = {}
    for attempt in range(6):
        time.sleep(10)
        status = dd("order", "status", "--order-uuid", order_uuid)
        final_status = status.get("status", "unknown")
        logging.info("status[%d]: %s", attempt, final_status)
        if final_status != "pending":
            break

    budget_line = f"\nCharged to: {budget.get('name','?')}" if budget else ""
    if final_status == "successful":
        show_alert("Lunchbot: order placed", f"{fav.store} — ${total_cents/100:.2f}{budget_line}")
    elif final_status == "action_required":
        show_alert("Lunchbot: verification needed",
                   f"{fav.store} — ${total_cents/100:.2f}{budget_line}\n\n"
                   "Open the DoorDash app to finish a verification step.")
    elif final_status == "failed":
        err = status.get("error_message", "")
        show_alert("Lunchbot: order failed",
                   f"{fav.store} — ${total_cents/100:.2f}{budget_line}\n\n"
                   + (err or "DoorDash marked it failed after submit."))
    else:
        show_alert(f"Lunchbot: status={final_status}",
                   f"{fav.store} — ${total_cents/100:.2f}{budget_line}")
    logging.info("done: %s (%s)", fav.store, final_status)
