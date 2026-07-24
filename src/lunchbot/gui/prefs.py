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
from ..config import (DIET_CHOICES, FULFILLMENT_CHOICES, Config, DesktopConfirmCfg,
                      Favorite, favorite_eligible, load_config, write_config)

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # no tcl-tk (needs `brew install python-tk@3.13`)
    tk = ttk = messagebox = None

LEAD_TIERS = [("Fast · 15 min", 15), ("Normal · 30 min", 30), ("Slow · 60 min", 60)]
DAY_NAMES = [("Mon", 1), ("Tue", 2), ("Wed", 3), ("Thu", 4), ("Fri", 5),
             ("Sat", 6), ("Sun", 7)]


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
        self.root.geometry("560x680")
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

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Restaurants to rotate", font=("", 13, "bold")).pack(anchor="w")
        sf, sf_body = _make_scroll_frame(outer, height=190)
        sf.pack(fill="x", pady=(4, 10))
        self.store_rows = []  # (store_dict, include_var, lead_var)
        for s in self.stores:
            row = ttk.Frame(sf_body)
            row.pack(fill="x", pady=1)
            old = prev_by_id.get(s["store_id"])
            inc = tk.BooleanVar(value=old is not None)
            ttk.Checkbutton(row, variable=inc).pack(side="left")
            items = ", ".join(s["items"][:2])
            ttk.Label(row, text=f"{s['store']}  ", width=22, anchor="w").pack(side="left")
            lead_default = old.lead_minutes if old else 30
            lead_var = tk.StringVar(value=self._tier_label(lead_default))
            ttk.OptionMenu(row, lead_var, lead_var.get(),
                           *[lbl for lbl, _ in LEAD_TIERS]).pack(side="left")
            ttk.Label(row, text=f"  {items[:28]}", foreground="#888").pack(side="left")
            self.store_rows.append((s, inc, lead_var))

        grid = ttk.Frame(outer)
        grid.pack(fill="x", pady=4)

        # Diet + fulfillment
        self.diet_var = tk.StringVar(value=(prev.diet if prev else "omnivore"))
        self.fulfil_var = tk.StringVar(value=(prev.fulfillment if prev else "pickup"))
        self._labeled_option(grid, 0, "Diet", self.diet_var, list(DIET_CHOICES))
        self._labeled_option(grid, 1, "Fulfillment", self.fulfil_var, list(FULFILLMENT_CHOICES))

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
                        variable=self.work_var).pack(anchor="w", pady=(8, 4))

        # Weekdays
        ttk.Label(outer, text="Days", font=("", 13, "bold")).pack(anchor="w", pady=(6, 2))
        days_row = ttk.Frame(outer)
        days_row.pack(anchor="w")
        prev_days = set(prev.weekdays) if prev else {1, 2, 3, 4, 5}
        self.day_vars = []
        for name, num in DAY_NAMES:
            v = tk.BooleanVar(value=num in prev_days)
            ttk.Checkbutton(days_row, text=name, variable=v).pack(side="left")
            self.day_vars.append((num, v))

        # Status + buttons
        self.status_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.status_var, foreground="#c60").pack(anchor="w", pady=(8, 2))
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(6, 0))
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
    @staticmethod
    def _tier_label(minutes: int) -> str:
        for lbl, val in LEAD_TIERS:
            if val == minutes:
                return lbl
        return LEAD_TIERS[1][0]  # default Normal

    @staticmethod
    def _lead_from_label(label: str) -> int:
        for lbl, val in LEAD_TIERS:
            if lbl == label:
                return val
        return 30

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
        favorites = []
        for s, inc, lead_var in self.store_rows:
            if inc.get():
                favorites.append(Favorite(
                    store=s["store"], store_id=s["store_id"], reorder_from=s["order_uuid"],
                    diet=self.diet_var.get(), lead_minutes=self._lead_from_label(lead_var.get())))
        if not favorites:
            messagebox.showwarning("Lunchbot", "Pick at least one restaurant.")
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
            diet=self.diet_var.get(), fulfillment=self.fulfil_var.get(),
            price_cap_cents=price_cents, dry_run=(self.prev.dry_run if self.prev else False),
            work_benefits=self.work_var.get(),
            default_tip_cents=(self.prev.default_tip_cents if self.prev else 0),
            lunch_time=self.lunch_var.get(), delivery_address_id=addr_id,
            delivery_address=addr_str, weekdays=weekdays, favorites=favorites,
            desktop_confirm=DesktopConfirmCfg(enabled=True, timeout_seconds=300, on_timeout="abort"),
        )
        self.save_btn.configure(state="disabled")
        self.status_var.set("Saving — checking each restaurant supports "
                            f"{cfg.fulfillment}…")
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
