"""Preferences window (Tkinter). Launched as its own process
(`python -m lunchbot.gui.prefs`) by the menu-bar app — never inside the rumps
run loop, because AppKit's NSApplication loop and Tk's mainloop can't coexist.

Loads the current config, lets the user edit everything the terminal wizard
covers, then writes config.toml and reinstalls the ordering agent on Save.
All data-gathering is shared with the wizard via setup_core.
"""

from __future__ import annotations

import sys
import threading

from .. import agent, ddcli, paths, setup_core
from ..config import (DEFAULT_LEAD_TIERS, DIET_CHOICES, Config, DesktopConfirmCfg,
                      Favorite, favorite_eligible, load_config, write_config)

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # no tcl-tk (needs `brew install python-tk@3.13`)
    tk = ttk = messagebox = None

TIER_NAMES = ["Fast", "Normal", "Slow"]     # maps to cfg.lead_tiers[fast|normal|slow]
CUSTOM = "Custom"
DAY_NAMES = [("Mon", 1), ("Tue", 2), ("Wed", 3), ("Thu", 4), ("Fri", 5),
             ("Sat", 6), ("Sun", 7)]


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _existing() -> Config | None:
    try:
        return load_config()
    except Exception:
        return None


def main() -> int:
    if tk is None:
        print("The preferences window needs Tk (install with "
              "`brew install python-tk@3.13`). Meanwhile use `lunchbot setup`.",
              file=sys.stderr)
        return 1
    PrefsWindow().run()
    return 0


def _make_scroll_frame(master, height=180):
    """A vertically scrollable frame (Canvas + inner frame). Returns (frame, body)
    where widgets are added to `body`. Defined as a function, not a module-level
    ttk subclass, so this module imports even where tcl-tk is absent."""
    frame = ttk.Frame(master)
    canvas = tk.Canvas(frame, height=height, highlightthickness=0)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas)
    body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw")
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")
    return frame, body


class PrefsWindow:
    def __init__(self):
        self.prev = _existing()
        self.stores: list[dict] = []
        self.addresses: list[dict] = []
        self._save_result: tuple[bool, str] | None = None

        self.root = tk.Tk()
        self.root.title("Lunchbot Preferences")
        self.root.geometry("640x760")
        self.root.minsize(600, 640)
        self._build_loading()
        # Fetch network data off the main thread; populate via after().
        threading.Thread(target=self._load_async, daemon=True).start()

    # ---- async load -----------------------------------------------------
    def _build_loading(self):
        self.loading = ttk.Frame(self.root, padding=24)
        self.loading.pack(fill="both", expand=True)
        self.loading_lbl = ttk.Label(self.loading, text="Checking dd-cli…")
        self.loading_lbl.pack(pady=20)

    def _load_async(self):
        res = setup_core.preflight(attempt_login=False)
        if not res.ok:
            self._after(lambda: self._show_preflight_error(res))
            return
        try:
            stores = setup_core.history_stores()
            addrs = setup_core.addresses()
        except ddcli.DdError as e:
            self._after(lambda: self._fatal(f"Couldn't read DoorDash data:\n{e}"))
            return
        self.stores, self.addresses = stores, addrs
        self._after(self._build_form)

    def _after(self, fn):
        self.root.after(0, fn)

    def _show_preflight_error(self, res):
        for w in self.loading.winfo_children():
            w.destroy()
        ttk.Label(self.loading, text="dd-cli isn't ready", font=("", 15, "bold")).pack(pady=(0, 8))
        msg = tk.Text(self.loading, height=10, wrap="word", relief="flat")
        msg.insert("1.0", res.detail)
        msg.configure(state="disabled")
        msg.pack(fill="both", expand=True, pady=8)
        row = ttk.Frame(self.loading)
        row.pack()
        if res.needs_login:
            ttk.Button(row, text="Open dd-cli login", command=self._do_login).pack(side="left", padx=4)
        ttk.Button(row, text="Retry", command=self._retry).pack(side="left", padx=4)
        ttk.Button(row, text="Close", command=self.root.destroy).pack(side="left", padx=4)

    def _do_login(self):
        threading.Thread(target=lambda: (ddcli.login_interactive(), self._after(self._retry)),
                         daemon=True).start()

    def _retry(self):
        for w in self.loading.winfo_children():
            w.destroy()
        self.loading_lbl = ttk.Label(self.loading, text="Checking dd-cli…")
        self.loading_lbl.pack(pady=20)
        threading.Thread(target=self._load_async, daemon=True).start()

    def _fatal(self, text):
        for w in self.loading.winfo_children():
            w.destroy()
        ttk.Label(self.loading, text=text, wraplength=480).pack(pady=20)
        ttk.Button(self.loading, text="Close", command=self.root.destroy).pack()

    # ---- form -----------------------------------------------------------
    def _build_form(self):
        self.loading.destroy()
        prev = self.prev
        prev_by_id = {f.store_id: f for f in prev.favorites} if prev else {}
        self.tiers = dict(prev.lead_tiers) if prev else dict(DEFAULT_LEAD_TIERS)

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        # ---- Restaurants (aligned grid, truncated columns) --------------
        ttk.Label(outer, text="Restaurants to rotate", font=("", 14, "bold")).pack(anchor="w")
        sf, body = _make_scroll_frame(outer, height=200)
        sf.pack(fill="x", pady=(4, 12))
        body.columnconfigure(1, minsize=170)
        body.columnconfigure(3, weight=1)
        hdr = ("", "Restaurant", "Speed", "Usual order")
        for c, txt in enumerate(hdr):
            ttk.Label(body, text=txt, foreground="#888").grid(row=0, column=c, sticky="w", padx=(0, 10))
        self.store_rows = []  # (store, include_var, tier_var, old_minutes)
        for i, s in enumerate(self.stores, start=1):
            old = prev_by_id.get(s["store_id"])
            inc = tk.BooleanVar(value=old is not None)
            ttk.Checkbutton(body, variable=inc).grid(row=i, column=0, sticky="w")
            ttk.Label(body, text=_trunc(s["store"], 22)).grid(row=i, column=1, sticky="w", padx=(0, 10))
            tier_var = tk.StringVar(value=self._tier_for(old.lead_minutes if old else self.tiers["normal"]))
            ttk.OptionMenu(body, tier_var, tier_var.get(), *(TIER_NAMES + [CUSTOM])) \
                .grid(row=i, column=2, sticky="w", padx=(0, 10))
            preview = ", ".join(s["items"][:2])
            ttk.Label(body, text=_trunc(preview, 30), foreground="#888") \
                .grid(row=i, column=3, sticky="w")
            self.store_rows.append((s, inc, tier_var, old.lead_minutes if old else None))

        # ---- Lead-time presets (editable) -------------------------------
        ttk.Label(outer, text="Lead-time presets (minutes before lunch)",
                  font=("", 12, "bold")).pack(anchor="w", pady=(2, 2))
        trow = ttk.Frame(outer)
        trow.pack(anchor="w", pady=(0, 12))
        self.tier_vars = {}
        for name in ("fast", "normal", "slow"):
            ttk.Label(trow, text=name.capitalize()).pack(side="left")
            v = tk.StringVar(value=str(self.tiers.get(name, DEFAULT_LEAD_TIERS[name])))
            ttk.Entry(trow, textvariable=v, width=5).pack(side="left", padx=(4, 16))
            self.tier_vars[name] = v

        grid = ttk.Frame(outer)
        grid.pack(fill="x", pady=4)

        # Diet
        self.diet_var = tk.StringVar(value=(prev.diet if prev else "omnivore"))
        self._labeled_option(grid, 0, "Diet", self.diet_var, list(DIET_CHOICES))

        # Fulfillment — pickup and/or delivery (both = pick best per restaurant)
        ful = prev.fulfillment if prev else "pickup"
        self.pickup_var = tk.BooleanVar(value=ful in ("pickup", "either"))
        self.delivery_var = tk.BooleanVar(value=ful in ("delivery", "either"))
        ttk.Label(grid, text="Fulfillment", width=16, anchor="w").grid(row=1, column=0, sticky="w", pady=2)
        fful = ttk.Frame(grid)
        fful.grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(fful, text="Pickup", variable=self.pickup_var).pack(side="left")
        ttk.Checkbutton(fful, text="Delivery", variable=self.delivery_var).pack(side="left", padx=(10, 0))
        ttk.Label(grid, text="both → best per restaurant", foreground="#888").grid(row=1, column=2, sticky="w")

        # Address
        self.addr_labels = ["(keep current)"] + [self._addr_label(a) for a in self.addresses]
        self.addr_var = tk.StringVar(value=self._preselect_addr())
        self._labeled_option(grid, 2, "Order to", self.addr_var, self.addr_labels)

        # Lunch time + price cap
        self.lunch_var = tk.StringVar(value=(prev.lunch_time if prev else "12:00"))
        self.price_var = tk.StringVar(value=str((prev.price_cap_cents // 100) if prev else 25))
        self._labeled_entry(grid, 3, "Lunch time (HH:MM)", self.lunch_var)
        self._labeled_entry(grid, 4, "Max price ($)", self.price_var)

        # Work benefits
        self.work_var = tk.BooleanVar(value=(prev.work_benefits if prev else True))
        ttk.Checkbutton(outer, text="Require a company work-benefit budget (fail without one)",
                        variable=self.work_var).pack(anchor="w", pady=(10, 4))

        # Weekdays
        ttk.Label(outer, text="Days", font=("", 14, "bold")).pack(anchor="w", pady=(8, 2))
        days_row = ttk.Frame(outer)
        days_row.pack(anchor="w")
        prev_days = set(prev.weekdays) if prev else {1, 2, 3, 4, 5}
        self.day_vars = []
        for name, num in DAY_NAMES:
            v = tk.BooleanVar(value=num in prev_days)
            ttk.Checkbutton(days_row, text=name, variable=v).pack(side="left", padx=(0, 4))
            self.day_vars.append((num, v))

        # Status + buttons
        self.status_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.status_var, foreground="#c60",
                  wraplength=580).pack(anchor="w", pady=(10, 2))
        btns = ttk.Frame(outer)
        btns.pack(fill="x", side="bottom", pady=(10, 0))
        self.save_btn = ttk.Button(btns, text="Save & install", command=self._on_save)
        self.save_btn.pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.root.destroy).pack(side="right", padx=6)

    def _labeled_option(self, grid, r, label, var, choices):
        ttk.Label(grid, text=label, width=16, anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        ttk.OptionMenu(grid, var, var.get(), *choices).grid(row=r, column=1, sticky="w")

    def _labeled_entry(self, grid, r, label, var):
        ttk.Label(grid, text=label, width=16, anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=var, width=12).grid(row=r, column=1, sticky="w")

    # ---- helpers --------------------------------------------------------
    def _tier_for(self, minutes: int) -> str:
        """Name of the tier whose configured minutes match, else Custom."""
        for name in ("fast", "normal", "slow"):
            if self.tiers.get(name) == minutes:
                return name.capitalize()
        return CUSTOM

    @staticmethod
    def _addr_label(a: dict) -> str:
        tag = f" [{a['label']}]" if a.get("label") else ""
        return f"{a.get('printable_address', '?')}{tag}"

    def _preselect_addr(self) -> str:
        if not self.prev or not self.prev.delivery_address_id:
            return "(keep current)"
        for a in self.addresses:
            if a.get("address_id") == self.prev.delivery_address_id:
                return self._addr_label(a)
        return "(keep current)"

    # ---- save -----------------------------------------------------------
    def _on_save(self):
        # Lead-time presets (validate ints).
        try:
            tiers = {name: int(self.tier_vars[name].get()) for name in ("fast", "normal", "slow")}
            if any(v <= 0 for v in tiers.values()):
                raise ValueError
        except ValueError:
            messagebox.showwarning("Lunchbot", "Lead-time presets must be positive whole minutes.")
            return

        favorites = []
        for s, inc, tier_var, old_minutes in self.store_rows:
            if not inc.get():
                continue
            name = tier_var.get()
            if name == CUSTOM:
                lead = old_minutes if old_minutes is not None else tiers["normal"]
            else:
                lead = tiers[name.lower()]
            favorites.append(Favorite(
                store=s["store"], store_id=s["store_id"], reorder_from=s["order_uuid"],
                diet=self.diet_var.get(), lead_minutes=lead))
        if not favorites:
            messagebox.showwarning("Lunchbot", "Pick at least one restaurant.")
            return

        # Fulfillment from the two checkboxes.
        p, d = self.pickup_var.get(), self.delivery_var.get()
        if p and d:
            fulfillment = "either"
        elif p:
            fulfillment = "pickup"
        elif d:
            fulfillment = "delivery"
        else:
            messagebox.showwarning("Lunchbot", "Choose pickup, delivery, or both.")
            return

        weekdays = sorted(num for num, v in self.day_vars if v.get()) or [1, 2, 3, 4, 5]
        try:
            price_cents = int(float(self.price_var.get())) * 100
        except ValueError:
            messagebox.showwarning("Lunchbot", "Max price must be a number.")
            return

        # Resolve address selection.
        addr_id, addr_str = (self.prev.delivery_address_id if self.prev else ""), \
                            (self.prev.delivery_address if self.prev else "")
        sel = self.addr_var.get()
        if sel != "(keep current)":
            for a in self.addresses:
                if self._addr_label(a) == sel:
                    addr_id, addr_str = a["address_id"], a.get("printable_address", "")

        cfg = Config(
            diet=self.diet_var.get(), fulfillment=fulfillment,
            price_cap_cents=price_cents, dry_run=(self.prev.dry_run if self.prev else False),
            work_benefits=self.work_var.get(),
            default_tip_cents=(self.prev.default_tip_cents if self.prev else 0),
            lunch_time=self.lunch_var.get(), delivery_address_id=addr_id,
            delivery_address=addr_str, weekdays=weekdays, lead_tiers=tiers,
            favorites=favorites,
            desktop_confirm=DesktopConfirmCfg(enabled=True, timeout_seconds=300, on_timeout="abort"),
        )
        self.save_btn.configure(state="disabled")
        self.status_var.set(f"Saving — checking each restaurant supports {fulfillment}…")
        threading.Thread(target=self._save_worker, args=(cfg, addr_id), daemon=True).start()
        self.root.after(150, self._poll_save)

    def _save_worker(self, cfg: Config, addr_id: str):
        try:
            if addr_id:
                try:
                    ddcli.set_address(addr_id)
                except ddcli.DdError:
                    pass
            kept = []
            for fav in cfg.favorites:
                keep, _ = setup_core.probe_favorite(cfg.fulfillment, cfg.lunch_time, fav)
                if keep:
                    kept.append(fav)
            if not kept:
                self._save_result = (False, f"No restaurant supports {cfg.fulfillment}.")
                return
            cfg.favorites = kept
            if not any(favorite_eligible(f.diet, cfg.diet) for f in cfg.favorites):
                self._save_result = (False, f"No restaurant satisfies a {cfg.diet} diet.")
                return
            write_config(cfg)
            agent.migrate_legacy()
            agent.install_agent(cfg)
            self._save_result = (True, "")
        except Exception as e:  # noqa: BLE001 — surface in the UI
            self._save_result = (False, str(e))

    def _poll_save(self):
        if self._save_result is None:
            self.root.after(150, self._poll_save)
            return
        ok, err = self._save_result
        if ok:
            messagebox.showinfo("Lunchbot", f"Saved. Config at {paths.CONFIG_PATH}")
            self.root.destroy()
        else:
            self.status_var.set(err)
            self.save_btn.configure(state="normal")
            self._save_result = None

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
