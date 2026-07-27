"""Preferences window (Tkinter). Launched as its own process
(`python -m lunchbot.gui.prefs`) by the menu-bar app — never inside the rumps
run loop, because AppKit's NSApplication loop and Tk's mainloop can't coexist.

Loads the current config, lets the user edit everything the terminal wizard
covers, then writes config.toml and reinstalls the ordering agent on Save.
All data-gathering is shared with the wizard via setup_core.
"""

from __future__ import annotations

import queue
import sys
import threading

from .. import agent, ddcli, paths, setup_core
from ..config import (DEFAULT_LEAD_TIERS, DIET_CHOICES, Config, DesktopConfirmCfg,
                      Favorite, favorite_eligible, load_config, write_config)
from ..state import set_schedule_paused

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # no tcl-tk (needs `brew install python-tk@3.13`)
    tk = ttk = messagebox = None

TIER_NAMES = ["Fast", "Normal", "Slow"]     # maps to cfg.lead_tiers[fast|normal|slow]
CUSTOM = "Custom"
DAY_NAMES = [("Mon", 1), ("Tue", 2), ("Wed", 3), ("Thu", 4), ("Fri", 5),
             ("Sat", 6), ("Sun", 7)]

# ---- design tokens (a small, self-contained visual system) ------------------
SURFACE = "#ffffff"   # single light surface for the whole window
SUBTLE  = "#f0f2f5"   # inactive tabs / progress trough / secondary button
TEXT    = "#1f2733"   # primary text
MUTED   = "#7c8593"   # secondary text
BORDER  = "#d7dbe0"   # hairline borders on fields
ACCENT  = "#2e9e6b"   # primary action (green, matching the app)
ACCENT_DK = "#26895c"
WARN    = "#c0562b"


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
    """A vertically scrollable frame with a fixed (non-scrolling) header row.
    Returns (frame, header, body): put column labels in `header` and the rows in
    `body`, and give both grids the same column configuration so the columns line
    up. Defined as a function, not a module-level ttk subclass, so this module
    imports even where tcl-tk is absent."""
    frame = ttk.Frame(master)
    header = ttk.Frame(frame)
    canvas = tk.Canvas(frame, height=height, highlightthickness=0, bg=SURFACE)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas)
    win = canvas.create_window((0, 0), window=body, anchor="nw")

    body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    # Keep the inner body as wide as the canvas so column weights fill the width
    # (and stay aligned with the header).
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
    canvas.configure(yscrollcommand=vsb.set)

    # Header sits above the canvas; the scrollbar spans only the scrolling row,
    # so header and canvas share column 0 and line up to the same width.
    header.grid(row=0, column=0, sticky="ew")
    canvas.grid(row=1, column=0, sticky="nsew")
    vsb.grid(row=1, column=1, sticky="ns")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(1, weight=1)

    # Trackpad / wheel scrolling whenever the pointer is over the list.
    def _on_wheel(event):
        if event.num == 4:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            canvas.yview_scroll(1, "units")
        elif event.delta:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _bind_wheel(_):
        canvas.bind_all("<MouseWheel>", _on_wheel)
        canvas.bind_all("<Button-4>", _on_wheel)
        canvas.bind_all("<Button-5>", _on_wheel)

    def _unbind_wheel(_):
        canvas.unbind_all("<MouseWheel>")
        canvas.unbind_all("<Button-4>")
        canvas.unbind_all("<Button-5>")

    canvas.bind("<Enter>", _bind_wheel)
    canvas.bind("<Leave>", _unbind_wheel)
    return frame, header, body


class PrefsWindow:
    def __init__(self):
        self.prev = _existing()
        self.stores: list[dict] = []
        self.addresses: list[dict] = []
        self._events: queue.Queue | None = None

        self.root = tk.Tk()
        self.root.title("Lunchbot Preferences")
        self.root.geometry("720x760")
        self.root.minsize(680, 680)
        self._setup_style()
        self._build_loading()
        # Fetch network data off the main thread; populate via after().
        threading.Thread(target=self._load_async, daemon=True).start()

    # ---- theming --------------------------------------------------------
    def _setup_style(self):
        """A flat, single-surface look built on the fully-styleable clam theme."""
        self.root.configure(bg=SURFACE)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=SURFACE, foreground=TEXT,
                        font=("", 12), focuscolor=SURFACE, bordercolor=BORDER)
        style.configure("TFrame", background=SURFACE)
        style.configure("TLabel", background=SURFACE, foreground=TEXT)
        style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED)
        style.configure("Warn.TLabel", background=SURFACE, foreground=WARN)
        style.configure("H1.TLabel", background=SURFACE, foreground=TEXT, font=("", 16, "bold"))
        style.configure("Section.TLabel", background=SURFACE, foreground=TEXT, font=("", 13, "bold"))
        style.configure("SubSection.TLabel", background=SURFACE, foreground=TEXT, font=("", 11, "bold"))

        style.configure("TCheckbutton", background=SURFACE, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", SURFACE)])

        style.configure("TEntry", fieldbackground=SURFACE, foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        insertcolor=TEXT, padding=5)
        style.map("TEntry", bordercolor=[("focus", ACCENT)])

        style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, arrowcolor=TEXT, padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", SURFACE)],
                  foreground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT)],
                  selectbackground=[("readonly", SURFACE)],
                  selectforeground=[("readonly", TEXT)])
        # Colour the drop-down list too (it's a classic Tk Listbox).
        self.root.option_add("*TCombobox*Listbox.background", SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        style.configure("TButton", background=SUBTLE, foreground=TEXT,
                        bordercolor=BORDER, relief="flat", padding=(14, 7))
        style.map("TButton", background=[("active", "#e5e8ee"), ("disabled", SUBTLE)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        bordercolor=ACCENT, relief="flat", padding=(18, 7),
                        font=("", 12, "bold"))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DK), ("disabled", "#b6d3c3")],
                  foreground=[("disabled", "#eef5f0")])

        style.configure("TNotebook", background=SURFACE, borderwidth=0,
                        tabmargins=(0, 6, 0, 0))
        style.configure("TNotebook.Tab", background=SUBTLE, foreground=MUTED,
                        bordercolor=SURFACE, padding=(18, 9), font=("", 12))
        style.map("TNotebook.Tab",
                  background=[("selected", SURFACE)],
                  foreground=[("selected", TEXT)])

        style.configure("Accent.Horizontal.TProgressbar", troughcolor=SUBTLE,
                        background=ACCENT, bordercolor=SUBTLE,
                        lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        style.configure("TSeparator", background=BORDER)
        style.configure("Vertical.TScrollbar", troughcolor=SURFACE,
                        background="#c9ced6", bordercolor=SURFACE,
                        arrowcolor=MUTED, relief="flat")

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
        ttk.Label(self.loading, text="dd-cli isn't ready", style="Section.TLabel").pack(pady=(0, 8))
        msg = tk.Text(self.loading, height=10, wrap="word", relief="flat",
                      bg=SURFACE, fg=TEXT, highlightthickness=0)
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

        content = ttk.Frame(self.root, padding=(18, 16))
        content.pack(fill="both", expand=True)

        # Footer (shared across tabs) is anchored to the bottom first, so the
        # notebook fills everything above it.
        footer = ttk.Frame(content)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Separator(content).pack(side="bottom", fill="x")

        nb = ttk.Notebook(content)
        nb.pack(side="top", fill="both", expand=True)
        rest_tab = ttk.Frame(nb, padding=(4, 14))
        settings_tab = ttk.Frame(nb, padding=(4, 14))
        nb.add(rest_tab, text="Restaurants")
        nb.add(settings_tab, text="Settings")

        self._build_restaurants_tab(rest_tab, prev_by_id)
        self._build_settings_tab(settings_tab, prev)

        # ---- footer: status + actions -----------------------------------
        self.save_btn = ttk.Button(footer, text="Save", style="Accent.TButton",
                                    command=self._on_save)
        self.save_btn.pack(side="right")
        ttk.Button(footer, text="Cancel", command=self.root.destroy).pack(side="right", padx=(0, 8))
        self.status_var = tk.StringVar(value="")
        ttk.Label(footer, textvariable=self.status_var, style="Warn.TLabel",
                  wraplength=440).pack(side="left", fill="x", expand=True)

    def _build_restaurants_tab(self, tab, prev_by_id):
        ttk.Label(tab, text="Rotate through these spots",
                  style="Section.TLabel").pack(anchor="w")
        ttk.Label(tab, text="Pick the restaurants to include and how early to order from each.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        sf, header, body = _make_scroll_frame(tab, height=260)
        sf.pack(fill="both", expand=True)
        # Identical column config on header and body keeps the columns aligned.
        for g in (header, body):
            g.columnconfigure(0, minsize=30)          # checkbox
            g.columnconfigure(1, minsize=190)         # restaurant
            g.columnconfigure(2, minsize=120)         # speed
            g.columnconfigure(3, weight=1)            # usual order (fills width)
        # Fixed char widths where content would otherwise outgrow the column and
        # break alignment with the header (the restaurant name varies per row).
        col_width = {1: 26}
        for c, txt in enumerate(("", "Restaurant", "Speed", "Usual order")):
            ttk.Label(header, text=txt, style="Muted.TLabel", width=col_width.get(c, 0)) \
                .grid(row=0, column=c, sticky="w", padx=(0, 12), pady=(0, 6))
        self.store_rows = []  # (store, include_var, tier_var, old_minutes)
        for i, s in enumerate(self.stores):
            old = prev_by_id.get(s["store_id"])
            inc = tk.BooleanVar(value=old is not None)
            ttk.Checkbutton(body, variable=inc).grid(row=i, column=0, sticky="w", pady=4)
            ttk.Label(body, text=_trunc(s["store"], 24), width=col_width[1]) \
                .grid(row=i, column=1, sticky="w", padx=(0, 12))
            tier_var = tk.StringVar(value=self._tier_for(old.lead_minutes if old else self.tiers["normal"]))
            ttk.Combobox(body, textvariable=tier_var, values=TIER_NAMES + [CUSTOM],
                         state="readonly", width=9) \
                .grid(row=i, column=2, sticky="w", padx=(0, 12))
            preview = ", ".join(s["items"][:2])
            ttk.Label(body, text=_trunc(preview, 32), style="Muted.TLabel") \
                .grid(row=i, column=3, sticky="w")
            self.store_rows.append((s, inc, tier_var, old.lead_minutes if old else None))

        # ---- Lead-time presets (editable) -------------------------------
        ttk.Separator(tab).pack(fill="x", pady=(14, 12))
        ttk.Label(tab, text="Lead-time presets (minutes before lunch)",
                  style="SubSection.TLabel").pack(anchor="w", pady=(0, 8))
        trow = ttk.Frame(tab)
        trow.pack(anchor="w")
        self.tier_vars = {}
        for name in ("fast", "normal", "slow"):
            ttk.Label(trow, text=name.capitalize()).pack(side="left")
            v = tk.StringVar(value=str(self.tiers.get(name, DEFAULT_LEAD_TIERS[name])))
            ttk.Entry(trow, textvariable=v, width=5).pack(side="left", padx=(6, 20))
            self.tier_vars[name] = v

    def _build_settings_tab(self, tab, prev):
        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        # Diet
        self.diet_var = tk.StringVar(value=(prev.diet if prev else "omnivore"))
        self._labeled_option(grid, 0, "Diet", self.diet_var, list(DIET_CHOICES), width=16)

        # Fulfillment — pickup and/or delivery (both = pick best per restaurant)
        ful = prev.fulfillment if prev else "pickup"
        self.pickup_var = tk.BooleanVar(value=ful in ("pickup", "either"))
        self.delivery_var = tk.BooleanVar(value=ful in ("delivery", "either"))
        self._row_label(grid, 1, "Fulfillment")
        fful = ttk.Frame(grid)
        fful.grid(row=1, column=1, sticky="w", pady=4)
        ttk.Checkbutton(fful, text="Pickup", variable=self.pickup_var).pack(side="left")
        ttk.Checkbutton(fful, text="Delivery", variable=self.delivery_var).pack(side="left", padx=(14, 0))
        ttk.Label(fful, text="both → best per restaurant", style="Muted.TLabel").pack(side="left", padx=(14, 0))

        # Address — fixed-width combobox so a long address can't overflow.
        self.addr_labels = ["(keep current)"] + [self._addr_label(a) for a in self.addresses]
        self.addr_var = tk.StringVar(value=self._preselect_addr())
        self._labeled_option(grid, 2, "Order to", self.addr_var, self.addr_labels, width=50)

        # Lunch time + price cap
        self.lunch_var = tk.StringVar(value=(prev.lunch_time if prev else "12:00"))
        self.price_var = tk.StringVar(value=str((prev.price_cap_cents // 100) if prev else 25))
        self._labeled_entry(grid, 3, "Lunch time (HH:MM)", self.lunch_var, width=10)
        self._labeled_entry(grid, 4, "Max price ($)", self.price_var, width=10)

        # Work benefits
        ttk.Separator(tab).pack(fill="x", pady=(16, 12))
        self.work_var = tk.BooleanVar(value=(prev.work_benefits if prev else True))
        ttk.Checkbutton(tab, text="Require a company work-benefit budget (fail without one)",
                        variable=self.work_var).pack(anchor="w")

        # Weekdays
        ttk.Separator(tab).pack(fill="x", pady=(16, 12))
        ttk.Label(tab, text="Days", style="SubSection.TLabel").pack(anchor="w", pady=(0, 8))
        days_row = ttk.Frame(tab)
        days_row.pack(anchor="w")
        prev_days = set(prev.weekdays) if prev else {1, 2, 3, 4, 5}
        self.day_vars = []
        for name, num in DAY_NAMES:
            v = tk.BooleanVar(value=num in prev_days)
            ttk.Checkbutton(days_row, text=name, variable=v).pack(side="left", padx=(0, 12))
            self.day_vars.append((num, v))

    def _row_label(self, grid, r, label):
        ttk.Label(grid, text=label, width=18, anchor="w") \
            .grid(row=r, column=0, sticky="w", pady=6)

    def _labeled_option(self, grid, r, label, var, choices, width=16):
        self._row_label(grid, r, label)
        ttk.Combobox(grid, textvariable=var, values=list(choices), state="readonly",
                     width=width).grid(row=r, column=1, sticky="w", pady=6)

    def _labeled_entry(self, grid, r, label, var, width=12):
        self._row_label(grid, r, label)
        ttk.Entry(grid, textvariable=var, width=width).grid(row=r, column=1, sticky="w", pady=6)

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
        self.status_var.set("")
        self.save_btn.configure(state="disabled")
        self._events = queue.Queue()
        self._open_progress()
        threading.Thread(target=self._save_worker, args=(cfg, addr_id), daemon=True).start()
        self.root.after(80, self._poll_save)

    # ---- progress dialog ------------------------------------------------
    def _open_progress(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Saving")
        dlg.configure(bg=SURFACE)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)   # no closing mid-save

        frm = ttk.Frame(dlg, padding=22)
        frm.pack(fill="both", expand=True)
        self._prog_title = ttk.Label(frm, text="Saving your preferences…", style="Section.TLabel")
        self._prog_title.pack(anchor="w")
        self._prog_bar = ttk.Progressbar(frm, mode="determinate", length=380,
                                         style="Accent.Horizontal.TProgressbar")
        self._prog_bar.pack(fill="x", pady=(16, 12))
        self._prog_msg = ttk.Label(frm, text="Getting started…", style="Muted.TLabel", wraplength=380)
        self._prog_msg.pack(anchor="w")
        self._prog_btns = ttk.Frame(frm)
        self._prog_btns.pack(side="bottom", anchor="e", pady=(18, 0))
        self._prog_dlg = dlg

        # Centre over the parent window.
        dlg.update_idletasks()
        w, h = 440, 210
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        dlg.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        try:  # modal, but never let a grab failure wedge the save
            dlg.grab_set()
        except tk.TclError:
            pass

    def _save_worker(self, cfg: Config, addr_id: str):
        q = self._events
        try:
            # Total phases: optional address + one probe per restaurant + diet
            # check + write config + install agent.
            total = (1 if addr_id else 0) + len(cfg.favorites) + 3
            q.put(("max", total))
            done = 0

            if addr_id:
                q.put(("text", "Setting your delivery address…"))
                try:
                    ddcli.set_address(addr_id)
                except ddcli.DdError:
                    pass
                done += 1
                q.put(("value", done))

            verb = {"pickup": "pickup", "delivery": "delivery", "either": "pickup or delivery"}[cfg.fulfillment]
            kept = []
            for fav in cfg.favorites:
                q.put(("text", f"Checking {fav.store} for {verb}…"))
                keep, _ = setup_core.probe_favorite(cfg.fulfillment, cfg.lunch_time, fav)
                if keep:
                    kept.append(fav)
                done += 1
                q.put(("value", done))
            if not kept:
                q.put(("done", False, f"No restaurant supports {cfg.fulfillment}.", ""))
                return
            cfg.favorites = kept

            q.put(("text", f"Confirming a {cfg.diet} diet is covered…"))
            if not any(favorite_eligible(f.diet, cfg.diet) for f in cfg.favorites):
                q.put(("done", False, f"No restaurant satisfies a {cfg.diet} diet.", ""))
                return
            done += 1
            q.put(("value", done))

            q.put(("text", "Writing your configuration…"))
            write_config(cfg)
            done += 1
            q.put(("value", done))

            q.put(("text", "Installing the ordering agent…"))
            agent.migrate_legacy()
            agent.install_agent(cfg)
            set_schedule_paused(False)   # a fresh save means the schedule is active
            done += 1
            q.put(("value", done))

            q.put(("done", True, "", str(paths.CONFIG_PATH)))
        except Exception as e:  # noqa: BLE001 — surface in the UI
            q.put(("done", False, str(e), ""))

    def _poll_save(self):
        try:
            while True:
                ev = self._events.get_nowait()
                kind = ev[0]
                if kind == "max":
                    self._prog_bar.configure(maximum=ev[1], value=0)
                elif kind == "value":
                    self._prog_bar.configure(value=ev[1])
                elif kind == "text":
                    self._prog_msg.configure(text=ev[1])
                elif kind == "done":
                    self._finish_save(ok=ev[1], err=ev[2], path=ev[3])
                    return
        except queue.Empty:
            pass
        self.root.after(80, self._poll_save)

    def _finish_save(self, ok: bool, err: str, path: str):
        if ok:
            self._prog_bar.configure(value=self._prog_bar["maximum"])
            self._prog_title.configure(text="Saved ✓")
            self._prog_msg.configure(text=f"Your lunch preferences are set.\nConfig saved to {path}")
            self._prog_dlg.protocol("WM_DELETE_WINDOW", self.root.destroy)
            ttk.Button(self._prog_btns, text="Done", style="Accent.TButton",
                       command=self.root.destroy).pack(side="right")
        else:
            def close():
                self._prog_dlg.grab_release()
                self._prog_dlg.destroy()
                self.save_btn.configure(state="normal")
                self.status_var.set(err)

            self._prog_title.configure(text="Couldn’t save")
            self._prog_msg.configure(text=err)
            self._prog_dlg.protocol("WM_DELETE_WINDOW", close)
            ttk.Button(self._prog_btns, text="Close", command=close).pack(side="right")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
