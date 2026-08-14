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
                    NSBezelBorder, NSBezierPath, NSBox, NSBoxSeparator, NSButton,
                    NSButtonTypeSwitch, NSColor, NSDatePicker,
                    NSForegroundColorAttributeName, NSFont, NSImage, NSImageView,
                    NSMenu, NSMenuItem, NSPopUpButton, NSProgressIndicator,
                    NSScrollView, NSSegmentedControl,
                    NSSegmentSwitchTrackingSelectAny,
                    NSSegmentSwitchTrackingSelectOne, NSTextAlignmentCenter,
                    NSTextAlignmentRight, NSTextField, NSUnderlineStyleAttributeName,
                    NSView, NSWindow, NSWindowStyleMaskClosable,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskTitled,
                    NSWorkspace)
from Foundation import (NSCalendar, NSCalendarUnitHour, NSCalendarUnitMinute,
                        NSDateComponents, NSMakeRect, NSMakeSize, NSObject,
                        NSTimer, NSURL)

from .. import agent, appbundle, ddcli, paths, setup_core
from ..config import (DEFAULT_LEAD_TIERS, Config, DesktopConfirmCfg, Favorite,
                      load_config, write_config)
from ..state import set_schedule_paused

# Spelled out rather than imported: these were renamed in the 10.14 SDK and the
# old names aren't guaranteed to be present in every pyobjc build.
DATE_PICKER_TEXTFIELD_AND_STEPPER = 0      # NSDatePickerStyleTextFieldAndStepper
DATE_PICKER_HOUR_MINUTE = 0x000C           # NSDatePickerElementFlagHourMinute
PROGRESS_STYLE_SPINNING = 1                # NSProgressIndicatorStyleSpinning
LINE_BREAK_TRUNCATING_TAIL = 4              # NSLineBreakByTruncatingTail
BOX_TYPE_CUSTOM = 4                         # NSBoxCustom
BOX_NO_TITLE = 0                            # NSNoTitle

# The only order-time choices offered — each favorite sets its own directly,
# no more separate customizable Fast/Normal/Slow presets. cfg.lead_tiers is
# left alone at the config layer (the terminal wizard still uses it); this
# window just stopped reading/writing it.
LEAD_OPTIONS = [15, 30, 45, 60]
ORDER_TIME_TOOLTIP = "Choose how early Lunchbot should place the order before your scheduled lunch."
DAY_NAMES = [("Mon", 1), ("Tue", 2), ("Wed", 3), ("Thu", 4), ("Fri", 5),
             ("Sat", 6), ("Sun", 7)]
KEEP_CURRENT = "(keep current)"
FAQ_URL = "https://github.com/kojipereira/homebrew-lunchbot#troubleshooting"

# Settings page: grouped card sections, each with an icon + title.
SECTION_PAD = 18       # inside a card, edge to first control
SECTION_GAP = 16       # between cards

# ---- geometry ---------------------------------------------------------------
# The window is a fixed size, like most Mac preference windows, so every frame
# below is absolute — no autoresizing masks to reason about. Long lists scroll.
W, H = 820, 800
PAD = 20
SWITCHER_TOP, SWITCHER_H = 14, 24
CONTENT_TOP = 54
FOOTER_H = 58
CONTENT_W = W - 2 * PAD
CONTENT_H = H - CONTENT_TOP - FOOTER_H
ROW_H = 36                       # one restaurant row — dropdown + "Orders at..." side by side


def _rect(x, top, w, h, container_h):
    """Frame from a top-left origin. AppKit measures y from the bottom, and a
    form is far easier to read laid out downwards."""
    return NSMakeRect(x, container_h - top - h, w, h)


def _pos(view, x, top, w, h, container_h):
    view.setFrame_(_rect(x, top, w, h, container_h))
    return view


def _label(text, size=None, bold=False, secondary=False, align_right=False, truncate=False):
    f = NSTextField.labelWithString_(text)
    size = size or NSFont.systemFontSize()
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
               else NSFont.systemFontOfSize_(size))
    if secondary:
        f.setTextColor_(NSColor.secondaryLabelColor())
    if align_right:
        f.setAlignment_(NSTextAlignmentRight)
    if truncate:
        # An ellipsis when the frame is narrower than the text, instead of a
        # hard cutoff mid-word at the frame's edge.
        f.cell().setLineBreakMode_(LINE_BREAK_TRUNCATING_TAIL)
    return f


def _small(text, **kw):
    return _label(text, size=NSFont.smallSystemFontSize(), **kw)


def _field(value=""):
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 22))
    f.setStringValue_(str(value))
    return f


def _checkbox(title, on, target=None, action=None):
    b = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 18))
    b.setButtonType_(NSButtonTypeSwitch)
    b.setTitle_(title)
    b.setState_(1 if on else 0)
    if target is not None:
        b.setTarget_(target)
        b.setAction_(action)
    return b


def _order_time_text(lunch_time_hhmm, lead_minutes):
    """"Orders at HH:MM AM/PM" for a lunch time minus a lead time, wrapping
    past midnight the same way the runtime's own lead-time math does."""
    h, m = (int(x) for x in lunch_time_hhmm.split(":"))
    total = (h * 60 + m - lead_minutes) % (24 * 60)
    oh, om = divmod(total, 60)
    suffix = "AM" if oh < 12 else "PM"
    return f"Orders at {oh % 12 or 12}:{om:02d} {suffix}"


def _closest_lead_option(minutes):
    return min(LEAD_OPTIONS, key=lambda m: abs(m - minutes))


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


def _card(top, h, container_h, w=None):
    """A rounded, bordered group box. NSBox's own fill/border colors take
    plain NSColor (no CGColorRef, so no dependency on the Quartz framework
    binding) — colors are semantic (separatorColor / controlBackgroundColor),
    so it still adapts to Dark Mode automatically, not a hand-picked color.
    Returns (box_to_add_to_the_page, content_view_to_add_children_to) — margins
    are zeroed so children can use the same absolute coordinates as everywhere
    else in this file, with no box-imposed inset to account for."""
    box = NSBox.alloc().initWithFrame_(_rect(0, top, w or CONTENT_W, h, container_h))
    box.setBoxType_(BOX_TYPE_CUSTOM)
    box.setTitlePosition_(BOX_NO_TITLE)
    box.setFillColor_(NSColor.controlBackgroundColor())
    box.setBorderColor_(NSColor.separatorColor())
    box.setBorderWidth_(1.0)
    box.setCornerRadius_(10.0)
    box.setContentViewMargins_((0.0, 0.0))
    return box, box.contentView()


def _icon(symbol_name, size=15):
    """An SF Symbol (built into macOS 11+, which Lunchbot already requires) —
    native, Dark-Mode-adaptive, no bundled asset to keep in sync."""
    iv = NSImageView.alloc().init()
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol_name, None)
    if img is not None:
        img.setSize_((size, size))
        iv.setImage_(img)
    iv.setFrame_(NSMakeRect(0, 0, size, size))
    return iv


def _section_header(card, symbol_name, title, card_h):
    """Icon + bold title at the top of a card. Returns the title label so a
    caller that needs an inline note (e.g. "(optional)") can measure it and
    place the note relative to its actual rendered width."""
    icon = _icon(symbol_name)
    icon.setFrame_(_rect(SECTION_PAD, SECTION_PAD + 2, 16, 16, card_h))
    card.addSubview_(icon)
    label = _label(title, bold=True)
    card.addSubview_(_pos(label, SECTION_PAD + 22, SECTION_PAD, 220, 18, card_h))
    return label


def _link_button(title, url):
    b = NSButton.buttonWithTitle_target_action_(title, _LinkOpener.shared(), "open:")
    b.setBezelStyle_(0)
    b.setBordered_(False)
    attributed = b.attributedTitle().mutableCopy()
    full_range = (0, attributed.length())
    attributed.addAttribute_value_range_(NSForegroundColorAttributeName,
                                         NSColor.linkColor(), full_range)
    attributed.addAttribute_value_range_(NSUnderlineStyleAttributeName, 1, full_range)
    b.setAttributedTitle_(attributed)
    # A genuine PyObjC-bridged NSButton (unlike a pure-Python NSObject
    # subclass) can't carry a new attribute — b._link_url = url raises
    # AttributeError. Keyed dict on the shared target instead, same pattern
    # as _order_time_labels above.
    _LinkOpener.shared()._urls[b] = url
    return b


class _LinkOpener(NSObject):
    """A single target for every link button — PyObjC targets are held
    weakly, so each button needs a target that outlives it; one shared
    instance avoids allocating (and leaking track of) one per link."""
    _instance = None

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls.alloc().init()
            cls._instance._urls = {}
        return cls._instance

    def open_(self, sender):
        url = self._urls.get(sender)
        if url:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))


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
        self.lunch_time = prev.lunch_time if prev else "12:00"
        self.store_rows = []
        self._order_time_labels = {}   # popup -> its "Orders at ..." label

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
                              0, 0, CONTENT_W - 100, 20, CONTENT_H))
        self.selected_count_label = _small("", secondary=True)
        self.selected_count_label.setAlignment_(NSTextAlignmentRight)
        self.selected_count_label.setFrame_(_rect(CONTENT_W - 100, 2, 100, 16, CONTENT_H))
        page.addSubview_(self.selected_count_label)
        page.addSubview_(_pos(_small("Pick the restaurants to include, and how early "
                                     "to order from each.", secondary=True),
                              0, 24, CONTENT_W, 16, CONTENT_H))

        # Column headers, aligned with the row geometry below.
        for x, title in ((28, "Restaurant"), (268, "Order time"), (576, "Usual order")):
            page.addSubview_(_pos(_small(title, secondary=True),
                                  x, 52, 180, 14, CONTENT_H))
        info = _small("ⓘ", secondary=True)
        info.setFrame_(_rect(268 + 78, 52, 14, 14, CONTENT_H))
        info.setToolTip_(ORDER_TIME_TOOLTIP)
        page.addSubview_(info)

        list_top = 70
        scroll_h = CONTENT_H - list_top - 8
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
            cb = _checkbox(_trunc(s["store"], 30), old is not None,
                           target=self, action="restaurantToggled:")
            cb.setFrame_(NSMakeRect(8, y + 4, 220, 18))
            doc.addSubview_(cb)

            titles = [f"{m} min before" for m in LEAD_OPTIONS]
            current_minutes = _closest_lead_option(old.lead_minutes if old else 30)
            pop = _popup(titles, LEAD_OPTIONS.index(current_minutes), width=140)
            pop.setFrame_(NSMakeRect(266, y, 140, 25))
            pop.setTarget_(self)
            pop.setAction_("orderTimeChanged:")
            doc.addSubview_(pop)

            # "Orders at HH:MM" sits beside the dropdown, not beneath it, so
            # rows stay a single line — the relationship reads left-to-right.
            order_time = _small(_order_time_text(self.lunch_time, current_minutes),
                                secondary=True)
            order_time.setFrame_(NSMakeRect(416, y + 6, 150, 14))
            doc.addSubview_(order_time)
            self._order_time_labels[pop] = order_time

            usual = _small(", ".join(s["items"][:2]), secondary=True, truncate=True)
            usual.setFrame_(NSMakeRect(576, y + 5, doc_w - 586, 16))
            doc.addSubview_(usual)

            self.store_rows.append((s, cb, pop))
        scroll.setDocumentView_(doc)
        page.addSubview_(scroll)
        self._update_selected_count()
        return page

    @objc.python_method
    def _build_settings_page(self):
        page = self._new_page()
        prev = self.prev
        # Body content aligns with the section title's text, not its icon —
        # SECTION_PAD is the icon's own left edge; indent is where the title
        # text next to it actually starts (see _section_header).
        indent = SECTION_PAD + 22
        right_margin = CONTENT_W - SECTION_PAD   # card's right inner edge
        iw = right_margin - indent               # usable width for indented content

        # ---- Card 1: Order preferences --------------------------------------
        card1_h = 250
        card1_box, card1 = _card(0, card1_h, CONTENT_H)
        page.addSubview_(card1_box)
        _section_header(card1, "fork.knife", "Order preferences", card1_h)

        ful = prev.fulfillment if prev else "pickup"
        card1.addSubview_(_pos(_label("Fulfillment"), indent, 54, 90, 18, card1_h))
        self.pickup_cb = _checkbox("Pickup", ful in ("pickup", "either"))
        self.pickup_cb.setFrame_(_rect(indent + 100, 54, 90, 18, card1_h))
        self.delivery_cb = _checkbox("Delivery", ful in ("delivery", "either"))
        self.delivery_cb.setFrame_(_rect(indent + 200, 54, 90, 18, card1_h))
        card1.addSubview_(self.pickup_cb)
        card1.addSubview_(self.delivery_cb)
        card1.addSubview_(_pos(_small("Both → whichever each restaurant supports.",
                                      secondary=True, truncate=True),
                               indent + 310, 55, right_margin - (indent + 310), 16, card1_h))

        self.addr_titles = _unique([KEEP_CURRENT]
                                   + [_addr_label(a) for a in self.addresses])
        card1.addSubview_(_pos(_label("Order to"), indent, 90, 150, 18, card1_h))
        self.addr_pop = _popup(self.addr_titles, self._preselect_addr(), width=iw)
        self.addr_pop.setFrame_(_rect(indent, 112, iw, 25, card1_h))
        card1.addSubview_(self.addr_pop)
        card1.addSubview_(_pos(_small("Where orders will be sent.", secondary=True),
                               indent, 140, iw, 16, card1_h))

        # Lunch time and Max price side by side.
        card1.addSubview_(_pos(_label("Lunch time"), indent, 168, 150, 18, card1_h))
        self.time_picker = NSDatePicker.alloc().initWithFrame_(
            _rect(indent, 190, 130, 24, card1_h))
        self.time_picker.setDatePickerStyle_(DATE_PICKER_TEXTFIELD_AND_STEPPER)
        self.time_picker.setDatePickerElements_(DATE_PICKER_HOUR_MINUTE)
        self.time_picker.setDateValue_(_time_to_date(prev.lunch_time if prev else "12:00"))
        card1.addSubview_(self.time_picker)
        card1.addSubview_(_pos(_small("Default time for daily orders.", secondary=True),
                               indent, 216, 300, 16, card1_h))

        price_x = indent + 380
        card1.addSubview_(_pos(_label("Max price"), price_x, 168, 150, 18, card1_h))
        card1.addSubview_(_pos(_label("$"), price_x, 193, 12, 18, card1_h))
        self.price_field = _field((prev.price_cap_cents // 100) if prev else 25)
        self.price_field.setFrame_(_rect(price_x + 14, 190, 80, 22, card1_h))
        card1.addSubview_(self.price_field)
        card1.addSubview_(_pos(_small("Only show restaurants at or below this price.",
                                      secondary=True, truncate=True),
                               price_x, 216, right_margin - price_x, 16, card1_h))

        # ---- Card 2: Company budget (optional) ------------------------------
        card2_top = card1_h + SECTION_GAP
        card2_h = 100
        card2_box, card2 = _card(card2_top, card2_h, CONTENT_H)
        page.addSubview_(card2_box)
        _section_header(card2, "building.2", "Company budget", card2_h)
        card2.addSubview_(_pos(_small("(optional)", secondary=True),
                               indent + 118, SECTION_PAD + 2, 80, 16, card2_h))

        self.work_cb = _checkbox("Require a company work-benefit budget",
                                 prev.work_benefits if prev else True)
        self.work_cb.setFrame_(_rect(indent, 54, iw, 18, card2_h))
        card2.addSubview_(self.work_cb)
        card2.addSubview_(_pos(_small("Orders will fail rather than charge your own card.",
                                      secondary=True),
                               indent, 76, iw, 16, card2_h))

        # ---- Card 3: Days ----------------------------------------------------
        card3_top = card2_top + card2_h + SECTION_GAP
        card3_h = 110
        card3_box, card3 = _card(card3_top, card3_h, CONTENT_H)
        page.addSubview_(card3_box)
        _section_header(card3, "calendar", "Days", card3_h)
        card3.addSubview_(_pos(_small("Days to place orders", secondary=True),
                               SECTION_PAD + 70, SECTION_PAD + 2, 200, 16, card3_h))

        # Two controls, not one — a visual break between weekdays and the
        # weekend reads faster than seven identical segments in a row.
        prev_days = set(prev.weekdays) if prev else {1, 2, 3, 4, 5}
        weekday_names, weekend_names = DAY_NAMES[:5], DAY_NAMES[5:]
        self.weekday_seg = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [n for n, _ in weekday_names], NSSegmentSwitchTrackingSelectAny, self, None)
        self.weekday_seg.setFrame_(_rect(indent, 54, 340, 26, card3_h))
        for i, (_n, num) in enumerate(weekday_names):
            self.weekday_seg.setSelected_forSegment_(num in prev_days, i)
        card3.addSubview_(self.weekday_seg)

        divider_x = indent + 340 + 14
        divider = NSBox.alloc().initWithFrame_(_rect(divider_x, 54, 1, 26, card3_h))
        divider.setBoxType_(NSBoxSeparator)
        card3.addSubview_(divider)

        self.weekend_seg = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [n for n, _ in weekend_names], NSSegmentSwitchTrackingSelectAny, self, None)
        self.weekend_seg.setFrame_(_rect(divider_x + 14, 54, 140, 26, card3_h))
        for i, (_n, num) in enumerate(weekend_names):
            self.weekend_seg.setSelected_forSegment_(num in prev_days, i)
        card3.addSubview_(self.weekend_seg)
        card3.addSubview_(_pos(_small("Lunchbot will only place orders on selected days.",
                                      secondary=True),
                               indent, 86, iw, 16, card3_h))

        # ---- Card 4: Order confirmation --------------------------------------
        card4_top = card3_top + card3_h + SECTION_GAP
        card4_h = 100
        card4_box, card4 = _card(card4_top, card4_h, CONTENT_H)
        page.addSubview_(card4_box)
        _section_header(card4, "bell", "Order confirmation", card4_h)

        self.yolo_cb = _checkbox("Yolo mode",
                                 (not prev.desktop_confirm.enabled) if prev else False)
        self.yolo_cb.setFrame_(_rect(indent, 54, iw, 18, card4_h))
        card4.addSubview_(self.yolo_cb)
        card4.addSubview_(_pos(_small("On = one restaurant picked at random each day and "
                                      "ordered at its own time, no dialog.",
                                      secondary=True, truncate=True),
                               indent, 76, iw, 16, card4_h))

        # ---- Footer: help link ------------------------------------------------
        footer_top = card4_top + card4_h + SECTION_GAP
        page.addSubview_(_pos(_small("Need help?", secondary=True),
                              0, footer_top + 3, 80, 16, CONTENT_H))
        link = _link_button("Visit our FAQ", FAQ_URL)
        link.setFrame_(_rect(78, footer_top, 140, 20, CONTENT_H))
        page.addSubview_(link)
        return page

    # ---- helpers --------------------------------------------------------
    def restaurantToggled_(self, _sender):
        self._update_selected_count()

    def orderTimeChanged_(self, sender):
        label = self._order_time_labels.get(sender)
        if label is not None:
            minutes = LEAD_OPTIONS[sender.indexOfSelectedItem()]
            label.setStringValue_(_order_time_text(self.lunch_time, minutes))

    @objc.python_method
    def _update_selected_count(self):
        n = sum(1 for _s, cb, _pop in self.store_rows if cb.state())
        self.selected_count_label.setStringValue_(f"{n} selected")

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
        favorites = []
        for s, cb, pop in self.store_rows:
            if not cb.state():
                continue
            lead = LEAD_OPTIONS[pop.indexOfSelectedItem()]
            favorites.append(Favorite(
                store=s["store"], store_id=s["store_id"],
                reorder_from=s["order_uuid"], lead_minutes=lead))
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

        selected = [num for i, (_n, num) in enumerate(DAY_NAMES[:5])
                   if self.weekday_seg.isSelectedForSegment_(i)]
        selected += [num for i, (_n, num) in enumerate(DAY_NAMES[5:])
                    if self.weekend_seg.isSelectedForSegment_(i)]
        weekdays = sorted(selected) or [1, 2, 3, 4, 5]

        # Address: index 0 is "(keep current)", so anything above it is a change.
        addr_id = self.prev.delivery_address_id if self.prev else ""
        addr_str = self.prev.delivery_address if self.prev else ""
        idx = self.addr_pop.indexOfSelectedItem()
        self.addr_changed = idx > 0
        if idx > 0:
            a = self.addresses[idx - 1]
            addr_id, addr_str = a["address_id"], a.get("printable_address", "")

        return Config(
            fulfillment=fulfillment, price_cap_cents=price_cents,
            # Config-file-only setting: carry it forward so saving prefs here
            # can't silently reset a value the user tuned in config.toml.
            max_pickup_miles=(self.prev.max_pickup_miles if self.prev else 1.0),
            dry_run=(self.prev.dry_run if self.prev else False),
            work_benefits=bool(self.work_cb.state()),
            default_tip_cents=(self.prev.default_tip_cents if self.prev else 0),
            lunch_time=_date_to_time(self.time_picker.dateValue()),
            delivery_address_id=addr_id, delivery_address=addr_str,
            weekdays=weekdays,
            lead_tiers=(dict(self.prev.lead_tiers) if self.prev else dict(DEFAULT_LEAD_TIERS)),
            favorites=favorites,
            desktop_confirm=DesktopConfirmCfg(enabled=not bool(self.yolo_cb.state()),
                                              timeout_seconds=300, on_timeout="abort"),
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
            total = (1 if self.addr_changed else 0) + len(cfg.favorites) + 2
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
                                                        cfg.lunch_time, fav,
                                                        cfg.max_pickup_miles)
                if keep:
                    kept.append(fav)
                done += 1
                q.put(("value", done))
            if not kept:
                q.put(("done", False, f"No restaurant supports {cfg.fulfillment}.", ""))
                return
            cfg.favorites = kept

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

DOCK_ICON_CORNER_RATIO = 0.1804   # approximates Apple's Big Sur+ icon corner radius


def _dock_icon_image():
    """A rounded-rect-clipped copy of the bundled icon, for
    setApplicationIconImage_. Unlike Finder/LaunchServices, that API shows
    the image exactly as given — it does not apply the usual squircle mask —
    so without this the Dock icon has hard square corners. Only worth doing
    here: this window runs with Regular activation policy (a real Dock/
    Cmd+Tab presence, shown as the underlying Python interpreter's own
    identity, not Lunchbot.app's — see appbundle.py's module docstring for
    why); the menu-bar app itself is an Accessory-policy app with no Dock
    icon at all, so the same fix there would have nothing to attach to."""
    if not appbundle.ICON_SOURCE.is_file():
        return None
    src = NSImage.alloc().initWithContentsOfFile_(str(appbundle.ICON_SOURCE))
    if src is None or not src.isValid():
        return None
    size = NSMakeSize(512, 512)
    rounded = NSImage.alloc().initWithSize_(size)
    rounded.lockFocus()
    rect = NSMakeRect(0, 0, 512, 512)
    radius = 512 * DOCK_ICON_CORNER_RATIO
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).addClip()
    src.drawInRect_(rect)
    rounded.unlockFocus()
    return rounded


def run() -> int:
    global _controller
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    icon = _dock_icon_image()
    if icon is not None:
        app.setApplicationIconImage_(icon)
    _install_main_menu(app)
    _controller = PrefsController.alloc().init()
    _controller.show()
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0
