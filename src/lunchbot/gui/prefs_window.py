"""The preferences window, built from stock AppKit controls (PyObjC).

Imported lazily by [prefs.py] — this module touches AppKit at import time, so
it only loads on a Mac with pyobjc present (which the Homebrew venv always has,
since rumps depends on it).

Everything here is a real macOS control — NSButton switches, NSPopUpButton,
NSDatePicker, NSSegmentedControl, NSScrollView — so the window inherits system
appearance, Dark Mode, the user's accent colour, and accessibility for free.
There is deliberately no theming code: `labelColor`/`secondaryLabelColor` and
the default control styles *are* the design.

Two PyObjC house rules are load-bearing here:

  * Every method on an NSObject subclass is published to the Objective-C runtime
    with underscores turned into colons, so a Python-only helper like
    `_build_form` has to be marked `@objc.python_method` or the class fails to
    build. Only the true entry points — `init`, the target/action methods, the
    NSTimer callbacks and the window delegate — are left undecorated.
  * AppKit is touched from the main thread only. Work runs on a background
    thread and comes back through a queue that an NSTimer drains.
"""

from __future__ import annotations

import queue
import threading

import objc
from AppKit import (NSAlert, NSApp, NSApplication,
                    NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
                    NSBezelBorder, NSBox, NSBoxSeparator, NSButton,
                    NSButtonTypeSwitch, NSColor, NSDatePicker, NSFont, NSMenu,
                    NSMenuItem, NSPopUpButton, NSProgressIndicator,
                    NSScrollView, NSSegmentedControl,
                    NSSegmentSwitchTrackingSelectAny,
                    NSSegmentSwitchTrackingSelectOne, NSTextAlignmentCenter,
                    NSTextAlignmentRight, NSTextField, NSView, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskTitled)
from Foundation import (NSCalendar, NSCalendarUnitHour, NSCalendarUnitMinute,
                        NSDateComponents, NSMakeRect, NSObject, NSTimer)

from .. import agent, ddcli, paths, setup_core
from ..config import (DEFAULT_LEAD_TIERS, DIET_CHOICES, Config,
                      DesktopConfirmCfg, Favorite, favorite_eligible,
                      load_config, write_config)
from ..state import set_schedule_paused

# Spelled out rather than imported: these were renamed in the 10.14 SDK and the
# old names aren't guaranteed to be present in every pyobjc build.
DATE_PICKER_TEXTFIELD_AND_STEPPER = 0      # NSDatePickerStyleTextFieldAndStepper
DATE_PICKER_HOUR_MINUTE = 0x000C           # NSDatePickerElementFlagHourMinute
PROGRESS_STYLE_SPINNING = 1                # NSProgressIndicatorStyleSpinning

TIER_NAMES = ["Fast", "Normal", "Slow"]     # maps to cfg.lead_tiers[fast|normal|slow]
CUSTOM = "Custom"
DAY_NAMES = [("Mon", 1), ("Tue", 2), ("Wed", 3), ("Thu", 4), ("Fri", 5),
             ("Sat", 6), ("Sun", 7)]
KEEP_CURRENT = "(keep current)"

# ---- geometry ---------------------------------------------------------------
# The window is a fixed size, like most Mac preference windows, so every frame
# below is absolute — no autoresizing masks to reason about. Long lists scroll.
W, H = 720, 660
PAD = 20
SWITCHER_TOP, SWITCHER_H = 14, 24
CONTENT_TOP = 54
FOOTER_H = 58
CONTENT_W = W - 2 * PAD
CONTENT_H = H - CONTENT_TOP - FOOTER_H
ROW_H = 30                       # one restaurant row
LABEL_COL_W = 150                # right-aligned label column on the Settings page
FIELD_X = LABEL_COL_W + 14


def _rect(x, top, w, h, container_h):
    """Frame from a top-left origin. AppKit measures y from the bottom, and a
    form is far easier to read laid out downwards."""
    return NSMakeRect(x, container_h - top - h, w, h)


def _pos(view, x, top, w, h, container_h):
    view.setFrame_(_rect(x, top, w, h, container_h))
    return view


def _label(text, size=None, bold=False, secondary=False, align_right=False):
    f = NSTextField.labelWithString_(text)
    size = size or NSFont.systemFontSize()
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
               else NSFont.systemFontOfSize_(size))
    if secondary:
        f.setTextColor_(NSColor.secondaryLabelColor())
    if align_right:
        f.setAlignment_(NSTextAlignmentRight)
    return f


def _small(text, **kw):
    return _label(text, size=NSFont.smallSystemFontSize(), **kw)


def _field(value=""):
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 22))
    f.setStringValue_(str(value))
    return f


def _checkbox(title, on):
    b = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 18))
    b.setButtonType_(NSButtonTypeSwitch)
    b.setTitle_(title)
    b.setState_(1 if on else 0)
    return b


def _popup(titles, selected_index=0, width=160):
    p = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(0, 0, width, 25), False)
    # Built through the menu rather than addItemWithTitle:, which silently
    # replaces same-titled entries — two saved addresses can print identically.
    for t in titles:
        item = NSMenuItem.alloc().init()
        item.setTitle_(t)
        p.menu().addItem_(item)
    if 0 <= selected_index < len(titles):
        p.selectItemAtIndex_(selected_index)
    return p


def _separator(x, top, w, container_h):
    b = NSBox.alloc().initWithFrame_(_rect(x, top, w, 1, container_h))
    b.setBoxType_(NSBoxSeparator)
    return b


def _push(title, target, action, default=False):
    b = NSButton.buttonWithTitle_target_action_(title, target, action)
    if default:
        b.setKeyEquivalent_("\r")
    return b


def _trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _unique(labels):
    """Disambiguate repeated menu titles so selecting by index stays honest."""
    seen, out = {}, []
    for s in labels:
        seen[s] = seen.get(s, 0) + 1
        out.append(s if seen[s] == 1 else f"{s} ({seen[s]})")
    return out


def _addr_label(a):
    tag = f" [{a['label']}]" if a.get("label") else ""
    return f"{a.get('printable_address', '?')}{tag}"


def _time_to_date(hhmm):
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
    except (ValueError, AttributeError):
        h, m = 12, 0
    comps = NSDateComponents.alloc().init()
    comps.setYear_(2000)
    comps.setMonth_(1)
    comps.setDay_(1)
    comps.setHour_(h)
    comps.setMinute_(m)
    return NSCalendar.currentCalendar().dateFromComponents_(comps)


def _date_to_time(date):
    comps = NSCalendar.currentCalendar().components_fromDate_(
        NSCalendarUnitHour | NSCalendarUnitMinute, date)
    return f"{comps.hour():02d}:{comps.minute():02d}"


class _Flipped(NSView):
    """Document view for the restaurant list: top-down coordinates, so rows lay
    out in reading order and the list opens scrolled to the top."""

    def isFlipped(self):
        return True


class PrefsController(NSObject):
    # ---- construction ---------------------------------------------------
    def init(self):
        self = objc.super(PrefsController, self).init()
        if self is None:
            return None
        try:
            self.prev = load_config()
        except Exception:  # noqa: BLE001 — "no config yet" is the normal first run
            self.prev = None
        self.stores = []
        self.addresses = []
        self.store_rows = []          # (store, checkbox, tier popup, old_minutes)
        self.events = None
        self.timer = None
        self.pages = []
        self.switcher = None
        self.addr_changed = False
        self._build_window()
        self._begin_load()
        return self

    @objc.python_method
    def _build_window(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("Lunchbot Preferences")
        self.window.setDelegate_(self)
        self.window.center()
        self.root = self.window.contentView()

        # The content area each page is swapped into.
        self.area = NSView.alloc().initWithFrame_(
            _rect(PAD, CONTENT_TOP, CONTENT_W, CONTENT_H, H))
        self.root.addSubview_(self.area)

        # Footer: hairline, status text, Cancel / Save.
        self.root.addSubview_(_separator(0, H - FOOTER_H, W, H))
        self.status = NSTextField.wrappingLabelWithString_("")
        self.status.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        self.status.setTextColor_(NSColor.systemRedColor())
        self.status.setFrame_(_rect(PAD, H - 44, W - 260, 34, H))
        self.root.addSubview_(self.status)

        self.save_btn = _push("Save", self, "save:", default=True)
        self.save_btn.setFrame_(_rect(W - PAD - 96, H - 44, 96, 32, H))
        self.cancel_btn = _push("Cancel", self, "cancel:")
        self.cancel_btn.setFrame_(_rect(W - PAD - 194, H - 44, 90, 32, H))
        for b in (self.cancel_btn, self.save_btn):
            b.setHidden_(True)
            self.root.addSubview_(b)

    @objc.python_method
    def show(self):
        self.window.makeKeyAndOrderFront_(None)

    # ---- page plumbing --------------------------------------------------
    @objc.python_method
    def _new_page(self):
        return NSView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT_W, CONTENT_H))

    @objc.python_method
    def _set_page(self, view):
        for v in list(self.area.subviews()):
            v.removeFromSuperview()
        self.area.addSubview_(view)

    @objc.python_method
    def _show_loading(self, text):
        page = self._new_page()
        spinner = NSProgressIndicator.alloc().initWithFrame_(
            _rect(CONTENT_W / 2 - 16, CONTENT_H / 2 - 44, 32, 32, CONTENT_H))
        spinner.setStyle_(PROGRESS_STYLE_SPINNING)
        spinner.setIndeterminate_(True)
        spinner.startAnimation_(None)
        page.addSubview_(spinner)
        lbl = _label(text, secondary=True)
        lbl.setAlignment_(NSTextAlignmentCenter)
        lbl.setFrame_(_rect(0, CONTENT_H / 2, CONTENT_W, 20, CONTENT_H))
        page.addSubview_(lbl)
        self._set_page(page)

    # ---- async load -----------------------------------------------------
    @objc.python_method
    def _begin_load(self, message="Checking dd-cli…", login_first=False):
        self._show_loading(message)
        q = queue.Queue()
        self.events = q
        target = self._login_worker if login_first else self._load_worker
        threading.Thread(target=target, args=(q,), daemon=True).start()
        self._start_timer("loadTick:")

    @objc.python_method
    def _load_worker(self, q):
        res = setup_core.preflight(attempt_login=False)
        if not res.ok:
            q.put(("preflight", res))
            return
        try:
            stores = setup_core.history_stores()
            addrs = setup_core.addresses()
        except ddcli.DdError as e:
            q.put(("fatal", f"Couldn't read DoorDash data:\n\n{e}"))
            return
        q.put(("loaded", stores, addrs))

    @objc.python_method
    def _login_worker(self, q):
        try:
            ddcli.login_interactive()
        except Exception:  # noqa: BLE001 — the reload reports whatever state we land in
            pass
        self._load_worker(q)

    @objc.python_method
    def _start_timer(self, selector):
        self._stop_timer()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.08, self, selector, None, True)

    @objc.python_method
    def _stop_timer(self):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None

    def loadTick_(self, _timer):
        try:
            ev = self.events.get_nowait()
        except queue.Empty:
            return
        self._stop_timer()
        if ev[0] == "preflight":
            self._show_preflight_error(ev[1])
        elif ev[0] == "fatal":
            self._show_message(ev[1], [("Close", "cancel:")])
        else:
            self.stores, self.addresses = ev[1], ev[2]
            self._build_form()

    # ---- error / message pages ------------------------------------------
    @objc.python_method
    def _show_message(self, text, buttons):
        page = self._new_page()
        body = NSTextField.wrappingLabelWithString_(text)
        body.setFrame_(_rect(0, 0, CONTENT_W, CONTENT_H - 60, CONTENT_H))
        page.addSubview_(body)
        x = CONTENT_W
        for title, action in reversed(buttons):
            b = _push(title, self, action)
            x -= 150
            b.setFrame_(_rect(x, CONTENT_H - 40, 142, 32, CONTENT_H))
            page.addSubview_(b)
        self._set_page(page)

    @objc.python_method
    def _show_preflight_error(self, res):
        buttons = [("Retry", "retry:"), ("Close", "cancel:")]
        if res.needs_login:
            buttons.insert(0, ("Open dd-cli login", "ddcliLogin:"))
        self._show_message(f"dd-cli isn’t ready.\n\n{res.detail}", buttons)

    def retry_(self, _sender):
        self._begin_load()

    def ddcliLogin_(self, _sender):
        self._begin_load("Waiting for the dd-cli browser login…", login_first=True)

    # ---- the form -------------------------------------------------------
    @objc.python_method
    def _build_form(self):
        prev = self.prev
        self.prev_by_id = {f.store_id: f for f in prev.favorites} if prev else {}
        self.tiers = dict(prev.lead_tiers) if prev else dict(DEFAULT_LEAD_TIERS)
        self.store_rows = []

        if self.switcher is None:
            self.switcher = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
                ["Restaurants", "Settings"], NSSegmentSwitchTrackingSelectOne,
                self, "switchPage:")
            self.switcher.setFrame_(
                _rect((W - 260) / 2, SWITCHER_TOP, 260, SWITCHER_H, H))
            self.root.addSubview_(self.switcher)
        self.switcher.setSelectedSegment_(0)

        self.pages = [self._build_restaurants_page(), self._build_settings_page()]
        self._set_page(self.pages[0])
        for b in (self.cancel_btn, self.save_btn):
            b.setHidden_(False)
        self.window.makeFirstResponder_(self.switcher)

    def switchPage_(self, sender):
        self._set_page(self.pages[sender.selectedSegment()])

    @objc.python_method
    def _build_restaurants_page(self):
        page = self._new_page()
        page.addSubview_(_pos(_label("Rotate through these spots", bold=True, size=14),
                              0, 0, CONTENT_W, 20, CONTENT_H))
        page.addSubview_(_pos(_small("Pick the restaurants to include, and how early "
                                     "to order from each.", secondary=True),
                              0, 24, CONTENT_W, 16, CONTENT_H))

        # Column headers, aligned with the row geometry below.
        for x, title in ((28, "Restaurant"), (268, "Speed"), (388, "Usual order")):
            page.addSubview_(_pos(_small(title, secondary=True),
                                  x, 52, 220, 14, CONTENT_H))

        list_top, presets_top = 70, CONTENT_H - 74
        scroll_h = presets_top - 14 - list_top
        scroll = NSScrollView.alloc().initWithFrame_(
            _rect(0, list_top, CONTENT_W, scroll_h, CONTENT_H))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(NSBezelBorder)

        doc_w = CONTENT_W - 18                        # leave room for the scroller
        doc_h = max(len(self.stores) * ROW_H + 8, scroll_h)
        doc = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, doc_w, doc_h))
        if not self.stores:
            empty = _label("No reorderable DoorDash history yet — place an order in "
                           "the DoorDash app first.", secondary=True)
            empty.setFrame_(NSMakeRect(12, 12, doc_w - 24, 20))
            doc.addSubview_(empty)
        for i, s in enumerate(self.stores):
            old = self.prev_by_id.get(s["store_id"])
            y = i * ROW_H + 4
            cb = _checkbox(_trunc(s["store"], 30), old is not None)
            cb.setFrame_(NSMakeRect(8, y + 4, 250, 18))
            doc.addSubview_(cb)

            titles = TIER_NAMES + [CUSTOM]
            current = self._tier_for(old.lead_minutes if old else self.tiers["normal"])
            pop = _popup(titles, titles.index(current), width=110)
            pop.setFrame_(NSMakeRect(266, y, 110, 25))
            doc.addSubview_(pop)

            usual = _small(_trunc(", ".join(s["items"][:2]), 36), secondary=True)
            usual.setFrame_(NSMakeRect(386, y + 5, doc_w - 396, 16))
            doc.addSubview_(usual)

            self.store_rows.append((s, cb, pop, old.lead_minutes if old else None))
        scroll.setDocumentView_(doc)
        page.addSubview_(scroll)

        page.addSubview_(_separator(0, presets_top - 14, CONTENT_W, CONTENT_H))
        page.addSubview_(_pos(_label("Lead-time presets", bold=True),
                              0, presets_top, 200, 18, CONTENT_H))
        page.addSubview_(_pos(_small("Minutes before lunch each speed fires.",
                                     secondary=True),
                              200, presets_top + 2, 320, 16, CONTENT_H))
        self.tier_fields = {}
        x = 0
        for name in ("fast", "normal", "slow"):
            page.addSubview_(_pos(_small(name.capitalize(), secondary=True),
                                  x, presets_top + 32, 52, 16, CONTENT_H))
            f = _field(self.tiers.get(name, DEFAULT_LEAD_TIERS[name]))
            f.setFrame_(_rect(x + 52, presets_top + 28, 56, 22, CONTENT_H))
            page.addSubview_(f)
            self.tier_fields[name] = f
            x += 130
        return page

    @objc.python_method
    def _build_settings_page(self):
        page = self._new_page()
        prev = self.prev

        def row_label(text, top):
            page.addSubview_(_pos(_label(text, align_right=True),
                                  0, top + 3, LABEL_COL_W, 18, CONTENT_H))

        # Diet
        diets = list(DIET_CHOICES)
        cur_diet = prev.diet if prev else "omnivore"
        row_label("Diet", 6)
        self.diet_pop = _popup(diets, diets.index(cur_diet) if cur_diet in diets else 0,
                               width=170)
        self.diet_pop.setFrame_(_rect(FIELD_X, 4, 170, 25, CONTENT_H))
        page.addSubview_(self.diet_pop)

        # Fulfillment
        ful = prev.fulfillment if prev else "pickup"
        row_label("Fulfillment", 44)
        self.pickup_cb = _checkbox("Pickup", ful in ("pickup", "either"))
        self.pickup_cb.setFrame_(_rect(FIELD_X, 44, 90, 18, CONTENT_H))
        self.delivery_cb = _checkbox("Delivery", ful in ("delivery", "either"))
        self.delivery_cb.setFrame_(_rect(FIELD_X + 96, 44, 90, 18, CONTENT_H))
        page.addSubview_(self.pickup_cb)
        page.addSubview_(self.delivery_cb)
        page.addSubview_(_pos(_small("Both → whichever each restaurant supports.",
                                     secondary=True),
                              FIELD_X + 200, 45, 300, 16, CONTENT_H))

        # Address
        self.addr_titles = _unique([KEEP_CURRENT]
                                   + [_addr_label(a) for a in self.addresses])
        row_label("Order to", 80)
        self.addr_pop = _popup(self.addr_titles, self._preselect_addr(),
                               width=CONTENT_W - FIELD_X)
        self.addr_pop.setFrame_(_rect(FIELD_X, 78, CONTENT_W - FIELD_X, 25, CONTENT_H))
        page.addSubview_(self.addr_pop)

        # Lunch time — a real time picker, not a string field.
        row_label("Lunch time", 118)
        self.time_picker = NSDatePicker.alloc().initWithFrame_(
            _rect(FIELD_X, 116, 110, 24, CONTENT_H))
        self.time_picker.setDatePickerStyle_(DATE_PICKER_TEXTFIELD_AND_STEPPER)
        self.time_picker.setDatePickerElements_(DATE_PICKER_HOUR_MINUTE)
        self.time_picker.setDateValue_(_time_to_date(prev.lunch_time if prev else "12:00"))
        page.addSubview_(self.time_picker)

        # Price cap
        row_label("Max price", 154)
        page.addSubview_(_pos(_label("$"), FIELD_X, 157, 12, 18, CONTENT_H))
        self.price_field = _field((prev.price_cap_cents // 100) if prev else 25)
        self.price_field.setFrame_(_rect(FIELD_X + 14, 154, 80, 22, CONTENT_H))
        page.addSubview_(self.price_field)

        page.addSubview_(_separator(0, 196, CONTENT_W, CONTENT_H))

        self.work_cb = _checkbox("Require a company work-benefit budget",
                                 prev.work_benefits if prev else True)
        self.work_cb.setFrame_(_rect(0, 212, 400, 18, CONTENT_H))
        page.addSubview_(self.work_cb)
        page.addSubview_(_pos(_small("Orders fail rather than charge your own card.",
                                     secondary=True),
                              20, 234, 420, 16, CONTENT_H))

        page.addSubview_(_separator(0, 268, CONTENT_W, CONTENT_H))

        page.addSubview_(_pos(_label("Days", bold=True), 0, 284, 100, 18, CONTENT_H))
        prev_days = set(prev.weekdays) if prev else {1, 2, 3, 4, 5}
        self.day_seg = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [n for n, _ in DAY_NAMES], NSSegmentSwitchTrackingSelectAny, self, None)
        self.day_seg.setFrame_(_rect(0, 308, 420, 26, CONTENT_H))
        for i, (_n, num) in enumerate(DAY_NAMES):
            self.day_seg.setSelected_forSegment_(num in prev_days, i)
        page.addSubview_(self.day_seg)
        return page

    # ---- helpers --------------------------------------------------------
    @objc.python_method
    def _tier_for(self, minutes):
        for name in ("fast", "normal", "slow"):
            if self.tiers.get(name) == minutes:
                return name.capitalize()
        return CUSTOM

    @objc.python_method
    def _preselect_addr(self):
        if not self.prev or not self.prev.delivery_address_id:
            return 0
        for i, a in enumerate(self.addresses):
            if a.get("address_id") == self.prev.delivery_address_id:
                return i + 1        # +1 for the "(keep current)" row
        return 0

    @objc.python_method
    def _warn(self, text):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Lunchbot")
        alert.setInformativeText_(text)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    # ---- actions --------------------------------------------------------
    def cancel_(self, _sender):
        self.window.close()

    def windowWillClose_(self, _note):
        NSApp().terminate_(None)

    def save_(self, _sender):
        cfg = self._collect()
        if cfg is None:
            return
        self.status.setStringValue_("")
        self.save_btn.setEnabled_(False)
        self.cancel_btn.setEnabled_(False)
        self.switcher.setEnabled_(False)
        q = queue.Queue()
        self.events = q
        self._show_saving()
        threading.Thread(target=self._save_worker,
                         args=(cfg, cfg.delivery_address_id, q),
                         daemon=True).start()
        self._start_timer("saveTick:")

    @objc.python_method
    def _collect(self):
        """Read the form into a Config, or show an alert and return None."""
        try:
            tiers = {n: int(self.tier_fields[n].stringValue().strip())
                     for n in ("fast", "normal", "slow")}
            if any(v <= 0 for v in tiers.values()):
                raise ValueError
        except ValueError:
            self._warn("Lead-time presets must be positive whole minutes.")
            return None

        diet = self.diet_pop.titleOfSelectedItem()
        favorites = []
        for s, cb, pop, old_minutes in self.store_rows:
            if not cb.state():
                continue
            name = pop.titleOfSelectedItem()
            if name == CUSTOM:
                lead = old_minutes if old_minutes is not None else tiers["normal"]
            else:
                lead = tiers[name.lower()]
            favorites.append(Favorite(
                store=s["store"], store_id=s["store_id"],
                reorder_from=s["order_uuid"], diet=diet, lead_minutes=lead))
        if not favorites:
            self._warn("Pick at least one restaurant.")
            return None

        p, d = bool(self.pickup_cb.state()), bool(self.delivery_cb.state())
        if p and d:
            fulfillment = "either"
        elif p:
            fulfillment = "pickup"
        elif d:
            fulfillment = "delivery"
        else:
            self._warn("Choose pickup, delivery, or both.")
            return None

        try:
            price_cents = int(float(self.price_field.stringValue().strip())) * 100
            if price_cents <= 0:
                raise ValueError
        except ValueError:
            self._warn("Max price must be a positive number of dollars.")
            return None

        weekdays = sorted(num for i, (_n, num) in enumerate(DAY_NAMES)
                          if self.day_seg.isSelectedForSegment_(i)) or [1, 2, 3, 4, 5]

        # Address: index 0 is "(keep current)", so anything above it is a change.
        addr_id = self.prev.delivery_address_id if self.prev else ""
        addr_str = self.prev.delivery_address if self.prev else ""
        idx = self.addr_pop.indexOfSelectedItem()
        self.addr_changed = idx > 0
        if idx > 0:
            a = self.addresses[idx - 1]
            addr_id, addr_str = a["address_id"], a.get("printable_address", "")

        return Config(
            diet=diet, fulfillment=fulfillment, price_cap_cents=price_cents,
            dry_run=(self.prev.dry_run if self.prev else False),
            work_benefits=bool(self.work_cb.state()),
            default_tip_cents=(self.prev.default_tip_cents if self.prev else 0),
            lunch_time=_date_to_time(self.time_picker.dateValue()),
            delivery_address_id=addr_id, delivery_address=addr_str,
            weekdays=weekdays, lead_tiers=tiers, favorites=favorites,
            desktop_confirm=DesktopConfirmCfg(enabled=True, timeout_seconds=300,
                                              on_timeout="abort"),
        )

    # ---- saving ---------------------------------------------------------
    @objc.python_method
    def _show_saving(self):
        page = self._new_page()
        top = CONTENT_H / 2 - 70
        self.prog_title = _label("Saving your preferences…", bold=True, size=14)
        self.prog_title.setFrame_(_rect(0, top, CONTENT_W, 20, CONTENT_H))
        page.addSubview_(self.prog_title)

        self.prog_bar = NSProgressIndicator.alloc().initWithFrame_(
            _rect(0, top + 32, CONTENT_W, 16, CONTENT_H))
        self.prog_bar.setIndeterminate_(False)
        self.prog_bar.setMinValue_(0.0)
        self.prog_bar.setMaxValue_(1.0)
        page.addSubview_(self.prog_bar)

        self.prog_msg = NSTextField.wrappingLabelWithString_("Getting started…")
        self.prog_msg.setTextColor_(NSColor.secondaryLabelColor())
        self.prog_msg.setFrame_(_rect(0, top + 58, CONTENT_W, 44, CONTENT_H))
        page.addSubview_(self.prog_msg)

        self.prog_page = page
        self.prog_top = top
        self._set_page(page)

    @objc.python_method
    def _save_worker(self, cfg, addr_id, q):
        try:
            total = (1 if self.addr_changed else 0) + len(cfg.favorites) + 3
            q.put(("max", total))
            done = 0

            if self.addr_changed and addr_id:
                q.put(("text", "Setting your delivery address…"))
                try:
                    ddcli.set_address(addr_id)
                except ddcli.DdError:
                    pass
                done += 1
                q.put(("value", done))

            verb = {"pickup": "pickup", "delivery": "delivery",
                    "either": "pickup or delivery"}[cfg.fulfillment]
            kept = []
            for fav in cfg.favorites:
                q.put(("text", f"Checking {fav.store} for {verb}…"))
                keep, _note = setup_core.probe_favorite(cfg.fulfillment,
                                                        cfg.lunch_time, fav)
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
            set_schedule_paused(False)     # a fresh save means the schedule is active
            done += 1
            q.put(("value", done))

            q.put(("done", True, "", str(paths.CONFIG_PATH)))
        except Exception as e:  # noqa: BLE001 — surface in the UI, never crash
            q.put(("done", False, str(e), ""))

    def saveTick_(self, _timer):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "max":
                self.prog_bar.setMaxValue_(float(ev[1]))
                self.prog_bar.setDoubleValue_(0.0)
            elif kind == "value":
                self.prog_bar.setDoubleValue_(float(ev[1]))
            elif kind == "text":
                self.prog_msg.setStringValue_(ev[1])
            elif kind == "done":
                self._stop_timer()
                self._finish_save(ev[1], ev[2], ev[3])
                return

    @objc.python_method
    def _finish_save(self, ok, err, path):
        if ok:
            self.prog_bar.setDoubleValue_(self.prog_bar.maxValue())
            self.prog_title.setStringValue_("Saved")
            self.prog_msg.setStringValue_(
                f"Your lunch preferences are set.\nConfig saved to {path}")
            done = _push("Done", self, "cancel:", default=True)
            done.setFrame_(_rect(CONTENT_W - 96, self.prog_top + 116, 96, 32, CONTENT_H))
            self.prog_page.addSubview_(done)
            self.window.makeFirstResponder_(done)
            self.save_btn.setHidden_(True)
            self.cancel_btn.setHidden_(True)
        else:
            self.status.setStringValue_(err)
            self.save_btn.setEnabled_(True)
            self.cancel_btn.setEnabled_(True)
            self.switcher.setEnabled_(True)
            self._set_page(self.pages[self.switcher.selectedSegment()])
            self._warn(f"Couldn’t save.\n\n{err}")


def _install_main_menu(app):
    """A minimal menu bar so ⌘W and ⌘Q behave the way every Mac user expects."""
    bar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    bar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_("Close", "performClose:", "w")
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_(
        "Quit Lunchbot Preferences", "terminate:", "q")
    app_item.setSubmenu_(app_menu)
    app.setMainMenu_(bar)


_controller = None   # module-level: AppKit holds targets weakly


def run() -> int:
    global _controller
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    _install_main_menu(app)
    _controller = PrefsController.alloc().init()
    _controller.show()
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0
