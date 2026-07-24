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

# ---- Diet model -------------------------------------------------------------
# A favorite is eligible when the diet its saved order satisfies is at least as
# strict as the user's preference: a vegan order is fine for a vegetarian or an
# omnivore; a vegetarian order is fine for an omnivore but not a vegan.
DIET_CHOICES = ("vegan", "vegetarian", "omnivore")
DIET_RANK = {"omnivore": 0, "vegetarian": 1, "vegan": 2}

_MEAT = (
    "chicken", "beef", "pork", "bacon", "ham", "sausage", "steak", "turkey",
    "duck", "lamb", "veal", "prosciutto", "salami", "pepperoni", "meatball",
    "carnitas", "chorizo", "brisket", "pastrami", "meatballs",
)
_SEAFOOD = (
    "fish", "salmon", "tuna", "shrimp", "prawn", "prawns", "crab", "lobster",
    "anchovy", "anchovies", "oyster", "oysters", "squid", "calamari",
    "scallop", "scallops", "clam", "clams", "mussel", "mussels",
)
_DAIRY_EGG = (
    "cheese", "butter", "cream", "milk", "yogurt", "egg", "eggs", "honey",
    "mayo", "mayonnaise", "parmesan", "mozzarella", "cheddar", "feta",
    "ranch", "aioli",
)

# Blocklist per diet. Word-boundary matched so "egg" won't hit "eggplant",
# "butter" won't hit "butternut", "ham" won't hit "hamburger".
_BLOCKLIST = {
    "vegan": _MEAT + _SEAFOOD + _DAIRY_EGG,
    "vegetarian": _MEAT + _SEAFOOD,
    "omnivore": (),
}
_BLOCK_RE = {
    diet: re.compile(r"\b(" + "|".join(re.escape(t) for t in toks) + r")\b", re.I)
    for diet, toks in _BLOCKLIST.items()
    if toks
}


@dataclass
class Favorite:
    store: str
    store_id: str
    reorder_from: str
    diet: str = "omnivore"      # what this store's saved order satisfies
    lead_minutes: int = 30


@dataclass
class DesktopConfirmCfg:
    enabled: bool = True
    timeout_seconds: int = 300
    on_timeout: str = "abort"   # "abort" | "approve"


FULFILLMENT_CHOICES = ("pickup", "delivery")


@dataclass
class Config:
    diet: str = "omnivore"      # the user's own dietary preference
    fulfillment: str = "pickup" # pickup | delivery
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
    favorites: list[Favorite] = field(default_factory=list)
    desktop_confirm: DesktopConfirmCfg = field(default_factory=DesktopConfirmCfg)


class ConfigError(Exception):
    """Raised with a human-readable message when config is missing/invalid."""


# ---- Diet helpers -----------------------------------------------------------
def favorite_eligible(fav_diet: str, user_diet: str) -> bool:
    return DIET_RANK.get(fav_diet, 0) >= DIET_RANK.get(user_diet, 0)


def diet_scan(items: list[dict], diet: str) -> list[str]:
    """Return a list of human-readable hits where an item name trips the
    diet blocklist. Empty list = passes. Safety net, not authoritative."""
    rx = _BLOCK_RE.get(diet)
    if rx is None:
        return []
    hits: list[str] = []
    for it in items:
        name = it.get("item", {}).get("name") or it.get("name") or ""
        m = rx.search(name)
        if m:
            hits.append(f"{name!r} contains {m.group(1)!r}")
    return hits


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
    def req(name, typ, default):
        v = raw.get(name, default)
        if not isinstance(v, typ):
            raise ConfigError(f"{p}: '{name}' must be {typ.__name__}, got {v!r}")
        return v

    diet = req("diet", str, "omnivore")
    if diet not in DIET_CHOICES:
        raise ConfigError(f"{p}: diet must be one of {DIET_CHOICES}, got {diet!r}")

    fulfillment = req("fulfillment", str, "pickup")
    if fulfillment not in FULFILLMENT_CHOICES:
        raise ConfigError(f"{p}: fulfillment must be one of {FULFILLMENT_CHOICES}, got {fulfillment!r}")

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

    favs_raw = raw.get("favorites", []) or []
    if not favs_raw:
        raise ConfigError(f"{p}: at least one [[favorites]] entry is required")
    favorites: list[Favorite] = []
    for i, f in enumerate(favs_raw):
        for key in ("store", "store_id", "reorder_from"):
            if not f.get(key):
                raise ConfigError(f"{p}: favorites[{i}] missing '{key}'")
        fdiet = f.get("diet", "omnivore")
        if fdiet not in DIET_CHOICES:
            raise ConfigError(f"{p}: favorites[{i}].diet must be one of {DIET_CHOICES}")
        favorites.append(Favorite(
            store=str(f["store"]),
            store_id=str(f["store_id"]),
            reorder_from=str(f["reorder_from"]),
            diet=fdiet,
            lead_minutes=int(f.get("lead_minutes", 30)),
        ))

    return Config(
        diet=diet,
        fulfillment=fulfillment,
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
    lines.append(f"diet = {_toml_str(cfg.diet)}              # vegan | vegetarian | omnivore")
    lines.append(f"fulfillment = {_toml_str(cfg.fulfillment)}  # pickup | delivery")
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
        lines.append(f"diet = {_toml_str(fav.diet)}")
        lines.append(f"lead_minutes = {fav.lead_minutes}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_config(cfg: Config, path=None) -> None:
    p = path or paths.CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_config(cfg))


def clone(cfg: Config, **changes) -> Config:
    return replace(cfg, **changes)
