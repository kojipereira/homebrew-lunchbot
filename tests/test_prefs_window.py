"""Exercise the AppKit preferences window against a PyObjC-shaped stub. Run with
the resolved interpreter:
    PYTHONPATH=src python3.13 tests/test_prefs_window.py
Exits non-zero on failure. No test framework, and no real AppKit — the stubs
below stand in for it, so this runs anywhere, including CI on Linux.

Checks four things that are otherwise unverifiable without a Mac in the loop:
  1. every undecorated method on an NSObject subclass has a legal selector arity
     — PyObjC turns underscores into colons and refuses a mismatch at class
     build time, so a stray helper without @objc.python_method is a hard crash
     the moment the module imports
  2. every target/action selector string points at a real method
  3. the form logic (build -> collect -> Config), including its refusals
  4. the async paths: preflight error, save progress, save completion
"""
import inspect
import os
import queue
import sys
import tempfile
import types
from pathlib import Path

# Isolate config/state on disk BEFORE importing lunchbot (paths reads env at import).
_tmp = tempfile.mkdtemp(prefix="lunchbot-test-")
os.environ["XDG_STATE_HOME"] = str(Path(_tmp) / "state")
os.environ["XDG_CONFIG_HOME"] = str(Path(_tmp) / "config")

failures = []


def check(cond, msg):
    print(("ok:   " if cond else "FAIL: ") + msg)
    if not cond:
        failures.append(msg)


# ---------------------------------------------------------------- fake objc
objc = types.ModuleType("objc")


def python_method(fn):
    fn.__pyobjc_python_method__ = True
    return fn


objc.python_method = python_method
objc.super = super
sys.modules["objc"] = objc

ACTIONS = []            # (target, selector) pairs registered via buttons/controls


class V:
    """Stand-in for any NSView / NSControl. Setters are no-ops; getters are real."""

    def __init__(self, string="", title=""):
        self._subviews = []
        self._string = string
        self._title = title
        self._state = 0
        self._items = []
        self._sel = 0
        self._segments = []
        self._segsel = set()
        self._date = "12:00"

    # -- generic no-op setter for everything not spelled out below
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None

    # -- view tree
    def addSubview_(self, v):
        self._subviews.append(v)

    def subviews(self):
        return list(self._subviews)

    def removeFromSuperview(self):
        pass

    def contentView(self):
        return self

    def cell(self):
        # Real AppKit: NSTextField/NSButton always have a backing cell. Return
        # self so a chained setter (e.g. setLineBreakMode_) is still a no-op
        # rather than an AttributeError on None.
        return self

    # -- text
    def stringValue(self):
        return self._string

    def setStringValue_(self, s):
        self._string = s

    # -- buttons
    def state(self):
        return self._state

    def setState_(self, s):
        self._state = s

    def setTitle_(self, t):
        self._title = t

    def title(self):
        return self._title

    # -- popup
    def menu(self):
        return self

    def addItem_(self, item):
        self._items.append(item)

    def selectItemAtIndex_(self, i):
        self._sel = i

    def indexOfSelectedItem(self):
        return self._sel

    def titleOfSelectedItem(self):
        return self._items[self._sel].title() if self._items else ""

    # -- segmented control
    def setSelected_forSegment_(self, on, i):
        (self._segsel.add if on else self._segsel.discard)(i)

    def isSelectedForSegment_(self, i):
        return i in self._segsel

    def setSelectedSegment_(self, i):
        self._sel = i

    def selectedSegment(self):
        return self._sel

    # -- date picker / progress
    def dateValue(self):
        return self._date

    def setDateValue_(self, d):
        self._date = d

    def maxValue(self):
        return 1.0

    # -- attributed-string chain (link buttons): attributedTitle().mutableCopy()
    # .addAttribute_value_range_(...) then setAttributedTitle_(...) — self stands
    # in for the attributed string too, since all that's exercised is the chain.
    def attributedTitle(self):
        return self

    def mutableCopy(self):
        return self

    def length(self):
        return len(self._title or self._string or "")

    def addAttribute_value_range_(self, *_a):
        pass

    def string(self):
        return self._title or self._string or ""

    def setWantsLayer_(self, _on):
        pass

    def layer(self):
        return self

    def CGColor(self):
        return self

    def frame(self):
        return types.SimpleNamespace(size=types.SimpleNamespace(width=0, height=0))


class Cls:
    """Stand-in for an AppKit class object."""

    def __init__(self, name):
        self._name = name

    def alloc(self):
        return self

    def __getattr__(self, name):
        def factory(*a, **k):
            if name in ("labelWithString_", "wrappingLabelWithString_"):
                return V(string=a[0], title=a[0])
            if name == "buttonWithTitle_target_action_":
                ACTIONS.append((a[1], a[2]))
                return V(title=a[0])
            if name == "segmentedControlWithLabels_trackingMode_target_action_":
                if a[3] is not None:
                    ACTIONS.append((a[2], a[3]))
                v = V()
                v._segments = list(a[0])
                return v
            return V()
        return factory


class NSObject:
    @classmethod
    def alloc(cls):
        return cls.__new__(cls)

    def init(self):
        return self


def _module(name, names, extra=None):
    m = types.ModuleType(name)
    for n in names:
        setattr(m, n, Cls(n))
    for k, v in (extra or {}).items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


TIMERS = []


class _Timer:
    @staticmethod
    def scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, target, selector, info, repeats):
        ACTIONS.append((target, selector))
        t = V()
        TIMERS.append((target, selector))
        return t


class _Font:
    @staticmethod
    def systemFontSize():
        return 13

    @staticmethod
    def smallSystemFontSize():
        return 11

    @staticmethod
    def systemFontOfSize_(s):
        return V()

    @staticmethod
    def boldSystemFontOfSize_(s):
        return V()


class _Color:
    @staticmethod
    def secondaryLabelColor():
        return V()

    @staticmethod
    def systemRedColor():
        return V()

    @staticmethod
    def separatorColor():
        return V()

    @staticmethod
    def controlBackgroundColor():
        return V()

    @staticmethod
    def linkColor():
        return V()


class _Cal:
    @staticmethod
    def currentCalendar():
        return _Cal()

    def dateFromComponents_(self, c):
        return f"{c.h:02d}:{c.m:02d}"

    def components_fromDate_(self, units, date):
        h, m = (int(x) for x in date.split(":"))
        return types.SimpleNamespace(hour=lambda: h, minute=lambda: m)


class _ProcessInfo:
    @staticmethod
    def processInfo():
        return V()


class _Comps:
    @staticmethod
    def alloc():
        return _Comps()

    def init(self):
        self.h = self.m = 0
        return self

    def setYear_(self, v):
        pass

    def setMonth_(self, v):
        pass

    def setDay_(self, v):
        pass

    def setHour_(self, v):
        self.h = v

    def setMinute_(self, v):
        self.m = v


_module("AppKit", [
    "NSAlert", "NSApplication", "NSBezierPath", "NSBox", "NSButton",
    "NSDatePicker", "NSImage", "NSImageView", "NSMenu", "NSMenuItem",
    "NSPopUpButton", "NSProgressIndicator", "NSScrollView", "NSSegmentedControl",
    "NSTextField", "NSView", "NSWindow", "NSWorkspace",
], extra={
    "NSApp": lambda: V(),
    "NSFont": _Font, "NSColor": _Color,
    "NSApplicationActivationPolicyRegular": 0, "NSBackingStoreBuffered": 2,
    "NSBezelBorder": 2, "NSBoxSeparator": 2, "NSButtonTypeSwitch": 1,
    "NSSegmentSwitchTrackingSelectAny": 1, "NSSegmentSwitchTrackingSelectOne": 0,
    "NSTextAlignmentCenter": 2, "NSTextAlignmentRight": 1,
    "NSWindowStyleMaskClosable": 2, "NSWindowStyleMaskMiniaturizable": 4,
    "NSWindowStyleMaskTitled": 1,
    "NSForegroundColorAttributeName": "NSForegroundColorAttributeName",
    "NSUnderlineStyleAttributeName": "NSUnderlineStyleAttributeName",
})
_module("Foundation", [], extra={
    "NSCalendar": _Cal, "NSCalendarUnitHour": 32, "NSCalendarUnitMinute": 64,
    "NSDateComponents": _Comps, "NSObject": NSObject, "NSTimer": _Timer,
    "NSProcessInfo": _ProcessInfo,
    "NSMakeRect": lambda x, y, w, h: (x, y, w, h),
    "NSMakeSize": lambda w, h: (w, h),
    "NSURL": Cls("NSURL"),
})


class NSViewBase(V):
    """NSView must be a real class — prefs_window subclasses it for _Flipped."""

    @classmethod
    def alloc(cls):
        return cls()

    def initWithFrame_(self, r):
        return self


sys.modules["AppKit"].NSView = NSViewBase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from lunchbot.gui import prefs_window as P  # noqa: E402

# ------------------------------------------------- 0. pure helper functions
print("\n--- order-time helpers ---")
check(P._order_time_text("12:00", 60) == "Orders at 11:00 AM",
      "60 min before a 12:00 PM lunch orders at 11:00 AM")
check(P._order_time_text("12:00", 15) == "Orders at 11:45 AM",
      "15 min before a 12:00 PM lunch orders at 11:45 AM")
check(P._order_time_text("00:10", 30) == "Orders at 11:40 PM",
      "lead time wraps past midnight")
check(P._closest_lead_option(20) == 15, "20 min rounds down to the 15 min option")
check(P._closest_lead_option(38) == 45, "38 min rounds up to the 45 min option")

# ------------------------------------------------- 1. selector arity
print("\n--- selector arity ---")
for klass in (P.PrefsController, P._Flipped):
    for name, fn in vars(klass).items():
        if not inspect.isfunction(fn) or name.startswith("__"):
            continue
        if getattr(fn, "__pyobjc_python_method__", False):
            continue
        nargs = len(inspect.signature(fn).parameters) - 1        # minus self
        colons = name.count("_")
        legal = colons == nargs and not name.startswith("_")
        check(legal, f"{klass.__name__}.{name}: selector "
                     f"'{name.replace('_', ':')}' takes {colons}, method takes {nargs}")
        spec = inspect.getfullargspec(fn)
        check(not spec.defaults and not spec.kwonlyargs,
              f"{klass.__name__}.{name}: no default/kw-only args on an ObjC method")

# ------------------------------------------------- 2 + 3. build the window
print("\n--- build + collect ---")
c = P.PrefsController.alloc().init()
check(c is not None, "controller initialised")

c.stores = [
    {"store": "Joe's Diner", "store_id": "1", "order_uuid": "u1",
     "items": ["Veg Bowl", "Fries"], "date": "2026-07-01"},
    {"store": "Taco Place", "store_id": "2", "order_uuid": "u2",
     "items": ["Bean Taco"], "date": "2026-07-02"},
]
c.addresses = [
    {"address_id": "a1", "printable_address": "1 Main St", "label": "home"},
    {"address_id": "a2", "printable_address": "1 Main St", "label": "home"},
]
c._build_form()
check(len(c.store_rows) == 2, "one row per store")
check(len(set(c.addr_titles)) == len(c.addr_titles),
      f"duplicate addresses disambiguated: {c.addr_titles}")
# The FAQ link button can't carry its URL as a bolted-on attribute on a real
# NSButton (only the fake stub here allows that) — it must go through
# _LinkOpener's dict instead. Assert the dict, not the button attribute, so
# this actually catches a regression back to the broken approach.
check(P.FAQ_URL in P._LinkOpener.shared()._urls.values(),
      "the FAQ link button is wired to FAQ_URL via _LinkOpener, not a bolted-on attribute")

# Nothing ticked yet -> refuses to save.
check(c._collect() is None, "no restaurants selected -> _collect() refuses")
check(c.selected_count_label.stringValue() == "0 selected",
      "selected count starts at 0")

# Tick both restaurants, pick delivery too, choose the second address.
for _s, cb, _pop in c.store_rows:
    cb.setState_(1)
    c.restaurantToggled_(cb)
check(c.selected_count_label.stringValue() == "2 selected",
      "selected count live-updates as restaurants are ticked")
c.delivery_cb.setState_(1)
c.addr_pop.selectItemAtIndex_(2)
c.time_picker.setDateValue_("11:30")
c.price_field.setStringValue_("30")
cfg = c._collect()
check(cfg is not None, "_collect() returns a Config once the form is valid")
check(cfg.fulfillment == "either", f"pickup+delivery -> either (got {cfg.fulfillment})")
check(cfg.lunch_time == "11:30", f"time picker round-trips (got {cfg.lunch_time})")
check(cfg.price_cap_cents == 3000, f"price -> cents (got {cfg.price_cap_cents})")
check(cfg.weekdays == [1, 2, 3, 4, 5], f"default weekdays (got {cfg.weekdays})")
check(cfg.delivery_address_id == "a2", f"second address chosen (got {cfg.delivery_address_id})")
check(c.addr_changed is True, "address change flagged for the save worker")
check([f.store for f in cfg.favorites] == ["Joe's Diner", "Taco Place"],
      "favorites carry the store names")
check(all(f.lead_minutes == 30 for f in cfg.favorites),
      "a new restaurant defaults to 30 min before")

# Bad numbers are rejected rather than written.
c.price_field.setStringValue_("abc")
check(c._collect() is None, "non-numeric price -> _collect() refuses")
c.price_field.setStringValue_("30")

# Deselect every restaurant AND every fulfillment mode.
c.pickup_cb.setState_(0)
c.delivery_cb.setState_(0)
check(c._collect() is None, "no fulfillment mode -> _collect() refuses")

# ------------------------------------------------- 4. async paths
print("\n--- preflight error page ---")
res = types.SimpleNamespace(ok=False, detail="not logged in", needs_login=True,
                            needs_dequarantine=False, dequarantine_cmd="")
c._show_preflight_error(res)
check(True, "_show_preflight_error builds without raising")

print("\n--- save progress + completion ---")
c._show_saving()
c.events = queue.Queue()
for ev in (("max", 4), ("text", "Checking Joe's Diner…"), ("value", 2)):
    c.events.put(ev)
c.saveTick_(None)
check(c.prog_msg.stringValue() == "Checking Joe's Diner…",
      "progress text reaches the label")
c.events.put(("done", True, "", "/tmp/config.toml"))
c.saveTick_(None)
check("/tmp/config.toml" in c.prog_msg.stringValue(),
      "success message names the config path")

c._show_saving()
c.events.put(("done", False, "dd-cli exploded", ""))
c.saveTick_(None)
check(c.status.stringValue() == "dd-cli exploded",
      "failure message lands in the footer status")

# ------------------------------------------------- action selectors resolve
print("\n--- target/action wiring ---")
for target, sel in ACTIONS:
    if target is not c:
        continue
    py = sel.rstrip(":").replace(":", "_") + ("_" if sel.endswith(":") else "")
    check(callable(getattr(c, py, None)), f"action '{sel}' -> method '{py}' exists")

print()
if failures:
    print(f"{len(failures)} FAILURES")
    sys.exit(1)
print("all stub checks passed")
