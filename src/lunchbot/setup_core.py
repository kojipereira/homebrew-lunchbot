"""UI-agnostic setup logic shared by the terminal wizard and the GUI.

None of these functions print or prompt — they return data or raise. The
terminal wizard ([wizard.py]) and the Tkinter preferences form
([gui/prefs.py]) both build their UI on top of these.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ddcli
from .config import Config, Favorite
from .order import delete_cart, prepare_candidate


@dataclass
class PreflightResult:
    ok: bool
    detail: str = ""          # human-readable status / next-step
    needs_login: bool = False
    needs_dequarantine: bool = False
    dequarantine_cmd: str = ""


def preflight(attempt_login: bool = False) -> PreflightResult:
    """Check dd-cli is present, un-quarantined, and logged in.

    If attempt_login is True and dd-cli isn't authed, kicks off the interactive
    browser login once and re-probes. Never prompts on the terminal itself.
    """
    exe = ddcli.which()
    if not exe:
        return PreflightResult(False, ddcli.ddcli_get_instructions())
    if ddcli.is_quarantined(exe):
        return PreflightResult(
            False,
            f"dd-cli is Gatekeeper-quarantined. Clear it with:\n    "
            f"{ddcli.dequarantine_hint(exe)}",
            needs_dequarantine=True,
            dequarantine_cmd=ddcli.dequarantine_hint(exe),
        )
    try:
        ddcli.login_probe()
        return PreflightResult(True, "dd-cli ready")
    except ddcli.TlsError as e:
        return PreflightResult(False, str(e))
    except ddcli.NotLoggedIn as e:
        if attempt_login and ddcli.login_interactive():
            try:
                ddcli.login_probe()
                return PreflightResult(True, "dd-cli ready")
            except ddcli.DdError as e2:
                return PreflightResult(False, f"Still not logged in: {e2}", needs_login=True)
        return PreflightResult(False, str(e), needs_login=True)
    except ddcli.DdError as e:
        return PreflightResult(False, f"dd-cli check inconclusive: {e}")


def history_stores() -> list[dict]:
    """Newest-first, deduped-by-store reorderable restaurant orders. Each:
    {store, store_id, order_uuid, items, date}. Raises ddcli.DdError."""
    resp = ddcli.dd("order", "history")
    orders = resp.get("orders", []) or []
    seen: set[str] = set()
    out: list[dict] = []
    for o in orders:
        if o.get("is_reorderable") is False:
            continue
        if o.get("order_target") and o["order_target"] != "ORDER_TARGET_RESTAURANT":
            continue
        sid = str(o.get("store_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append({
            "store": o.get("store_name", "?"),
            "store_id": sid,
            "order_uuid": o.get("order_uuid", ""),
            "items": [i.get("name", "?") for i in o.get("items", [])],
            "date": (o.get("order_date") or "")[:10],
        })
    return out


def probe_favorite(fulfillment: str, lunch_time: str, fav: Favorite) -> tuple[bool, str]:
    """Live-probe one favorite for the chosen fulfillment mode. Returns
    (keep, note). keep=True with an empty note on success; keep=True with a note
    on an inconclusive error (retry at runtime); keep=False when unsupported.
    Never leaks a cart."""
    probe_cfg = Config(fulfillment=fulfillment,
                       price_cap_cents=10_000_000, work_benefits=False,
                       lunch_time=lunch_time, favorites=[])
    try:
        cand, reason = prepare_candidate(probe_cfg, fav)
    except ddcli.DdError as e:
        return True, f"probe error ({e}) — keeping, will retry at runtime"
    if cand:
        delete_cart(cand[0])
        return True, ""
    return False, reason


def addresses() -> list[dict]:
    """Saved delivery addresses (may raise ddcli.DdError)."""
    return ddcli.list_addresses()
