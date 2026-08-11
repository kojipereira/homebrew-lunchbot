"""Config: dataclasses + TOML (stdlib only).

Read with tomllib (3.11+). tomllib is read-only, so setup writes via a small
purpose-built emitter. All mutable data (cursor/orders/skip_dates) lives in
state.json, so this file is written once at setup and never round-tripped.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace

from . import paths


@dataclass
class Favorite:
    store: str
    store_id: str
    reorder_from: str
    lead_minutes: int = 30


@dataclass
class DesktopConfirmCfg:
    enabled: bool = True
    timeout_seconds: int = 300
    on_timeout: str = "abort"   # "abort" | "approve"


# "either" = allow both; at order time lunchbot picks whatever the restaurant
# supports, preferring pickup (cheaper, no tip) when both are available.
FULFILLMENT_CHOICES = ("pickup", "delivery", "either")

# Named lead-time presets (minutes before lunch to fire). User-editable; a
# favorite stores the resolved minutes, so runtime never needs these.
DEFAULT_LEAD_TIERS = {"fast": 15, "normal": 30, "slow": 60}


@dataclass
class Config:
    fulfillment: str = "pickup" # pickup | delivery | either
    # "either" only: DoorDash reports pickup as offered at stores miles away, so
    # without a limit the cheaper pickup always wins and you get a long trip.
    # Past this many miles, skip pickup and let delivery take it. An explicit
    # fulfillment of "pickup" or "delivery" is respected and never overridden.
    max_pickup_miles: float = 1.0
    price_cap_cents: int = 2500
    dry_run: bool = False
    work_benefits: bool = False
    default_tip_cents: int = 0
    lunch_time: str = "12:00"   # HH:MM local; lead_minutes counts back from here
    default_expense_code: str = ""
    default_expense_note: str = ""
    delivery_address_id: str = ""
    delivery_address: str = ""  # printable_address for display; address_id is the key
    weekdays: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])  # 1=Mon..7=Sun
    lead_tiers: dict = field(default_factory=lambda: dict(DEFAULT_LEAD_TIERS))
    favorites: list[Favorite] = field(default_factory=list)
    desktop_confirm: DesktopConfirmCfg = field(default_factory=DesktopConfirmCfg)


class ConfigError(Exception):
    """Raised with a human-readable message when config is missing/invalid."""


# ---- Load -------------------------------------------------------------------
def load_config(path=None) -> Config:
    p = path or paths.CONFIG_PATH
    if not p.exists():
        raise ConfigError(
            f"No config at {p}. Run `lunchbot setup` first."
        )
    try:
        raw = tomllib.loads(p.read_text())
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ConfigError(f"Could not parse {p}: {e}") from e
    return _build_and_validate(raw, p)


def _build_and_validate(raw: dict, p) -> Config:
    """Unknown keys are ignored, so configs written by older versions (which
    carried a `diet` on the config and on each favorite) still load."""
    def req(name, typ, default):
        v = raw.get(name, default)
        if not isinstance(v, typ):
            raise ConfigError(f"{p}: '{name}' must be {typ.__name__}, got {v!r}")
        return v

    fulfillment = req("fulfillment", str, "pickup")
    if fulfillment not in FULFILLMENT_CHOICES:
        raise ConfigError(f"{p}: fulfillment must be one of {FULFILLMENT_CHOICES}, got {fulfillment!r}")

    max_pickup_miles = raw.get("max_pickup_miles", 1.0)
    if isinstance(max_pickup_miles, bool) or not isinstance(max_pickup_miles, (int, float)) \
            or max_pickup_miles <= 0:
        raise ConfigError(
            f"{p}: 'max_pickup_miles' must be a positive number, got {max_pickup_miles!r}")

    lunch_time = req("lunch_time", str, "12:00")
    if not re.fullmatch(r"[0-2]?\d:[0-5]\d", lunch_time):
        raise ConfigError(f"{p}: lunch_time must be HH:MM, got {lunch_time!r}")

    weekdays = req("weekdays", list, [1, 2, 3, 4, 5])
    if not weekdays or any(d not in range(1, 8) for d in weekdays):
        raise ConfigError(f"{p}: weekdays must be a non-empty list of 1..7 (Mon..Sun)")

    dc_raw = raw.get("desktop_confirm", {}) or {}
    on_timeout = dc_raw.get("on_timeout", "abort")
    if on_timeout not in ("abort", "approve"):
        raise ConfigError(f"{p}: desktop_confirm.on_timeout must be 'abort' or 'approve'")
    dc = DesktopConfirmCfg(
        enabled=bool(dc_raw.get("enabled", True)),
        timeout_seconds=int(dc_raw.get("timeout_seconds", 300)),
        on_timeout=on_timeout,
    )

    lt_raw = raw.get("lead_tiers", {}) or {}
    lead_tiers = dict(DEFAULT_LEAD_TIERS)
    for name in DEFAULT_LEAD_TIERS:
        if name in lt_raw:
            try:
                lead_tiers[name] = int(lt_raw[name])
            except (TypeError, ValueError):
                raise ConfigError(f"{p}: lead_tiers.{name} must be an integer")

    favs_raw = raw.get("favorites", []) or []
    if not favs_raw:
        raise ConfigError(f"{p}: at least one [[favorites]] entry is required")
    favorites: list[Favorite] = []
    for i, f in enumerate(favs_raw):
        for key in ("store", "store_id", "reorder_from"):
            if not f.get(key):
                raise ConfigError(f"{p}: favorites[{i}] missing '{key}'")
        favorites.append(Favorite(
            store=str(f["store"]),
            store_id=str(f["store_id"]),
            reorder_from=str(f["reorder_from"]),
            lead_minutes=int(f.get("lead_minutes", 30)),
        ))

    return Config(
        fulfillment=fulfillment,
        max_pickup_miles=float(max_pickup_miles),
        price_cap_cents=int(raw.get("price_cap_cents", 2500)),
        dry_run=bool(raw.get("dry_run", False)),
        work_benefits=bool(raw.get("work_benefits", False)),
        default_tip_cents=int(raw.get("default_tip_cents", 0)),
        lunch_time=lunch_time,
        default_expense_code=str(raw.get("default_expense_code", "")),
        default_expense_note=str(raw.get("default_expense_note", "")),
        delivery_address_id=str(raw.get("delivery_address_id", "")),
        delivery_address=str(raw.get("delivery_address", "")),
        weekdays=[int(d) for d in weekdays],
        lead_tiers=lead_tiers,
        favorites=favorites,
        desktop_confirm=dc,
    )


# ---- Write (hand-rolled TOML emitter) --------------------------------------
def _toml_str(s: str) -> str:
    """Emit a TOML basic string, escaping backslash and double-quote.
    Basic strings (not literal '...') so apostrophes like Mr. Charlie's are safe."""
    out = str(s).replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


def _toml_bool(b: bool) -> str:
    return "true" if b else "false"


def dump_config(cfg: Config) -> str:
    lines: list[str] = []
    lines.append("# lunchbot config — regenerate any time with `lunchbot setup`.")
    lines.append("# Mutable data (skip_dates, rotation) lives in state.json, not here.")
    lines.append("")
    lines.append(f"fulfillment = {_toml_str(cfg.fulfillment)}  # pickup | delivery | either")
    lines.append(f"max_pickup_miles = {cfg.max_pickup_miles}  # 'either' only: farther than "
                 "this and lunchbot delivers instead of sending you across town")
    lines.append(f"price_cap_cents = {cfg.price_cap_cents}   # skip any order over this")
    lines.append(f"dry_run = {_toml_bool(cfg.dry_run)}       # true = preview only, never submit")
    lines.append(f"work_benefits = {_toml_bool(cfg.work_benefits)}  # require a company budget or fail")
    lines.append(f"default_tip_cents = {cfg.default_tip_cents}")
    lines.append(f"lunch_time = {_toml_str(cfg.lunch_time)}  # HH:MM; lead times count back from here")
    lines.append(f"default_expense_code = {_toml_str(cfg.default_expense_code)}")
    lines.append(f"default_expense_note = {_toml_str(cfg.default_expense_note)}")
    lines.append(f"delivery_address_id = {_toml_str(cfg.delivery_address_id)}")
    lines.append(f"delivery_address = {_toml_str(cfg.delivery_address)}  # display only; address_id is the key")
    lines.append(f"weekdays = [{', '.join(str(d) for d in cfg.weekdays)}]  # 1=Mon .. 7=Sun")
    lines.append("")
    lines.append("[lead_tiers]              # minutes before lunch each preset fires")
    for name in ("fast", "normal", "slow"):
        lines.append(f"{name} = {int(cfg.lead_tiers.get(name, DEFAULT_LEAD_TIERS[name]))}")
    lines.append("")
    lines.append("[desktop_confirm]")
    lines.append(f"enabled = {_toml_bool(cfg.desktop_confirm.enabled)}")
    lines.append(f"timeout_seconds = {cfg.desktop_confirm.timeout_seconds}")
    lines.append(f"on_timeout = {_toml_str(cfg.desktop_confirm.on_timeout)}  # abort | approve")
    lines.append("")
    for fav in cfg.favorites:
        lines.append("[[favorites]]")
        lines.append(f"store = {_toml_str(fav.store)}")
        lines.append(f"store_id = {_toml_str(fav.store_id)}")
        lines.append(f"reorder_from = {_toml_str(fav.reorder_from)}")
        lines.append(f"lead_minutes = {fav.lead_minutes}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_config(cfg: Config, path=None) -> None:
    p = path or paths.CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_config(cfg))


def clone(cfg: Config, **changes) -> Config:
    return replace(cfg, **changes)
