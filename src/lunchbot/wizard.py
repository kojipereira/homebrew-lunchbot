"""`lunchbot setup` — interactive terminal wizard.

Re-runnable: if a config already exists, every prompt pre-fills with the
current value, so re-running edits preferences instead of starting over.
Reads the user's OWN dd-cli order history to build favorites, probes each for
pickup capability live, then installs the agent and fires an attended dry-run
(which triggers the Automation-TCC, keychain, and Gatekeeper prompts while the
user is present rather than silently at 11am).
"""

from __future__ import annotations

from datetime import date

from . import agent, ddcli, paths, setup_core
from .config import (FULFILLMENT_CHOICES, Config, DesktopConfirmCfg, Favorite,
                     load_config, write_config)
from .run import run
from .state import load_state
from .ui import (ask, ask_bool, ask_choice, ask_int, ask_multiselect, tcc_probe)

LEAD_TIERS = {"fast": 15, "normal": 30, "slow": 60}
DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


def _existing() -> Config | None:
    try:
        return load_config()
    except Exception:
        return None


def _preflight_ddcli() -> bool:
    """Terminal-facing wrapper over setup_core.preflight (attends login)."""
    res = setup_core.preflight(attempt_login=True)
    if not res.ok:
        print(f"\n{res.detail}")
    return res.ok


def setup() -> int:
    paths.ensure_dirs()
    prev = _existing()
    print("── lunchbot setup ──")
    if prev:
        print("Existing config found — press Enter to keep current values.\n")

    if not _preflight_ddcli():
        return 1

    print("\nReading your DoorDash order history…")
    try:
        stores = setup_core.history_stores()
    except ddcli.DdError as e:
        print(f"Could not read order history: {e}")
        return 1
    if not stores:
        print("No reorderable restaurant orders found in your history.")
        return 1

    prev_by_id = {f.store_id: f for f in prev.favorites} if prev else {}
    labels = []
    for s in stores:
        items = ", ".join(s["items"][:3]) + ("…" if len(s["items"]) > 3 else "")
        mark = " (current favorite)" if s["store_id"] in prev_by_id else ""
        labels.append(f"{s['store']} — {items} [{s['date']}]{mark}")

    chosen = ask_multiselect("\nWhich restaurants should lunchbot rotate through?", labels)
    if not chosen:
        print("No restaurants selected — nothing to do.")
        return 1

    # Fulfillment: pickup or delivery
    fulfillment = ask_choice("\nPickup or delivery?", list(FULFILLMENT_CHOICES),
                             prev.fulfillment if prev else "pickup")

    favorites: list[Favorite] = []
    print("\nFor each restaurant: how far ahead to order.")
    for idx in chosen:
        s = stores[idx]
        old = prev_by_id.get(s["store_id"])
        default_tier = next((k for k, v in LEAD_TIERS.items() if old and old.lead_minutes == v),
                            "normal")
        tier = ask_choice(f"  {s['store']} — lead time", list(LEAD_TIERS) + ["custom"], default_tier)
        lead = ask_int(f"  {s['store']} — minutes ahead", old.lead_minutes if old else 30) \
            if tier == "custom" else LEAD_TIERS[tier]
        favorites.append(Favorite(store=s["store"], store_id=s["store_id"],
                                   reorder_from=s["order_uuid"], lead_minutes=lead))

    # Delivery address (shown for both pickup and delivery — sets which stores are nearby)
    delivery_address_id = prev.delivery_address_id if prev else ""
    delivery_address = prev.delivery_address if prev else ""
    print("\nFetching your saved addresses…")
    try:
        addresses = ddcli.list_addresses()
    except ddcli.DdError as e:
        print(f"  Could not fetch addresses: {e}")
        addresses = []
    if addresses:
        addr_labels = []
        prev_idx = None
        for i, a in enumerate(addresses):
            label = a.get("label") or ""
            default_mark = " (current default)" if a.get("is_default") else ""
            tag = f" [{label}]" if label else ""
            addr_labels.append(f"{a.get('printable_address', '?')}{tag}{default_mark}")
            if delivery_address_id and a.get("address_id") == delivery_address_id:
                prev_idx = i
            elif prev_idx is None and a.get("is_default"):
                prev_idx = i
        addr_labels_with_skip = addr_labels + ["Keep current / skip"]
        addr_prompt = ("Where should lunchbot deliver to?"
                       if fulfillment == "delivery"
                       else "Which location for pickup? (determines nearby stores)")
        print(f"\n{addr_prompt} (This sets your DoorDash default address)")
        for i, lbl in enumerate(addr_labels_with_skip, 1):
            print(f"  {i}. {lbl}")
        default_choice = (prev_idx + 1) if prev_idx is not None else 1
        raw = ask(f"Choice [1-{len(addr_labels_with_skip)}]", str(default_choice))
        try:
            choice = int(raw)
        except ValueError:
            choice = default_choice
        if 1 <= choice <= len(addresses):
            picked = addresses[choice - 1]
            delivery_address_id = picked["address_id"]
            delivery_address = picked.get("printable_address", "")
            try:
                ddcli.set_address(delivery_address_id)
                print(f"  Set address: {delivery_address}")
            except ddcli.DdError as e:
                print(f"  Warning: could not set address: {e}")
        else:
            print("  Keeping current address.")

    # Global settings
    print()
    lunch_time = ask("Target lunch time (HH:MM)", prev.lunch_time if prev else "12:00")
    price_dollars = ask_int("Max price per order ($)",
                            (prev.price_cap_cents // 100) if prev else 25)
    work_benefits = ask_bool("Require a company work-benefit budget (fail without one)?",
                             prev.work_benefits if prev else True)
    confirm_before_ordering = ask_bool(
        "Confirm each order before it's placed? (No = Yolo mode, orders go through automatically)",
        prev.desktop_confirm.enabled if prev else True)

    # Weekday selection
    prev_weekdays = prev.weekdays if prev else [1, 2, 3, 4, 5]
    day_labels = [f"{DAY_NAMES[d]} ({d})" for d in range(1, 8)]
    pre_selected = [d - 1 for d in prev_weekdays]
    chosen_days = ask_multiselect("\nWhich days should lunchbot order?", day_labels,
                                  pre_selected=pre_selected)
    weekdays = sorted(d + 1 for d in chosen_days) if chosen_days else [1, 2, 3, 4, 5]

    cfg = Config(
        fulfillment=fulfillment,
        price_cap_cents=price_dollars * 100, dry_run=False,
        work_benefits=work_benefits, default_tip_cents=prev.default_tip_cents if prev else 0,
        lunch_time=lunch_time, default_expense_code="", default_expense_note="",
        delivery_address_id=delivery_address_id, delivery_address=delivery_address,
        weekdays=weekdays, favorites=favorites,
        desktop_confirm=DesktopConfirmCfg(enabled=confirm_before_ordering,
                                          timeout_seconds=300, on_timeout="abort"),
    )

    # Live fulfillment probe — drop stores that don't support the chosen mode.
    print(f"\nChecking each restaurant supports {fulfillment} right now…")
    kept: list[Favorite] = []
    for fav in favorites:
        keep, note = setup_core.probe_favorite(fulfillment, lunch_time, fav)
        if keep:
            print(f"  ✓ {fav.store}" + (f" — {note}" if note else ""))
            kept.append(fav)
        else:
            print(f"  ✗ {fav.store}: {note} — dropped")
    if not kept:
        print(f"None of the selected restaurants support {fulfillment}. Nothing to save.")
        return 1
    cfg.favorites = kept

    write_config(cfg)
    print(f"\nSaved config → {paths.CONFIG_PATH}")

    # Automation consent, attended.
    granted, detail = tcc_probe()
    if not granted:
        print(f"\nHeads up — {detail}")

    # Install the launchd agent automatically.
    agent.migrate_legacy()
    try:
        agent.install_agent(cfg)
        times = ", ".join(f"{h:02d}:{m:02d}" for h, m in agent.fire_times(cfg))
        wd = ", ".join(DAY_NAMES[d] for d in cfg.weekdays)
        print(f"\nAgent installed — fires at {times} on {wd}.")
    except RuntimeError as e:
        print(f"\nAgent install failed: {e}")

    # Attended test fire: proves the pipeline + triggers TCC/keychain/Gatekeeper now.
    print("\nRunning a test (dry-run, nothing is charged)…")
    state = load_state()
    try:
        run(cfg, state, force_pick=cfg.favorites[0].store, dry_run_override=True)
    except Exception as e:
        print(f"Test run error: {e}")

    print("\nDone. Useful commands:")
    print("  lunchbot doctor           # health check")
    print("  lunchbot run --dry-run    # preview today's pick")
    print("  lunchbot status           # schedule + last order")
    print("  lunchbot setup            # re-run to change preferences")
    return 0
