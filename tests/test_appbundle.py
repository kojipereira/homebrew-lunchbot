"""Lunchbot.app's Finder/Dock icon: the .icns ships, and install_app puts it
in the bundle. Run with the resolved interpreter:
    PYTHONPATH=src python3.13 tests/test_appbundle.py
Exits non-zero on failure. Writes only into a temp dir — safe on a live machine,
and it never touches ~/Applications.
"""

import plistlib
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot import appbundle as AB  # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")
    else:
        print(f"ok:   {msg}")


def icns_ostypes(data: bytes) -> dict[str, int]:
    """Walk the icns container and return {OSType: payload length}."""
    magic, total = struct.unpack(">4sI", data[:8])
    assert magic == b"icns", "not an icns file"
    assert total == len(data), f"header says {total} bytes, file is {len(data)}"
    out, pos = {}, 8
    while pos < len(data):
        ostype, size = struct.unpack(">4sI", data[pos:pos + 8])
        out[ostype.decode("ascii")] = size - 8
        pos += size
    return out


# --- the committed artifact --------------------------------------------------
check(AB.ICON_SOURCE.is_file(), f"{AB.ICON_SOURCE.name} is committed under src/")

raw = AB.ICON_SOURCE.read_bytes() if AB.ICON_SOURCE.is_file() else b""
try:
    types = icns_ostypes(raw)
    check(True, "icns container is well-formed (magic + self-consistent length)")
except (AssertionError, struct.error) as e:
    types = {}
    check(False, f"icns container is well-formed: {e}")

# Retina Dock/Finder/Get Info all read from these; a partial set makes the icon
# fall back to a blurry upscale at whichever size is missing.
for ostype in ("icp4", "icp5", "ic07", "ic08", "ic09", "ic10", "ic11", "ic12",
               "ic13", "ic14"):
    check(ostype in types, f"icns carries the {ostype} representation")
check(all(v > 0 for v in types.values()), "no empty representations in the icns")
check(raw[8 + 8:8 + 12] == b"\x89PNG" if len(raw) > 20 else False,
      "representations are PNG (what macOS 11+ expects)")

# --- install_app wires it into the bundle ------------------------------------
with tempfile.TemporaryDirectory(prefix="lunchbot-test-") as tmp:
    app = AB.install_app(Path(tmp))
    icon = app / "Contents" / "Resources" / AB.ICON_FILE
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())

    check(icon.is_file(), "install_app copies the icns into Contents/Resources")
    check(icon.read_bytes() == raw, "the installed icns is byte-identical to the source")
    check(info.get("CFBundleIconFile") == AB.ICON_FILE,
          f"Info.plist points CFBundleIconFile at {AB.ICON_FILE}")

    # Re-running after an upgrade must not leave a half-written bundle.
    AB.install_app(Path(tmp))
    check(icon.read_bytes() == raw, "install_app is idempotent (icon survives a re-run)")

# --- degrades instead of dangling -------------------------------------------
# If the resource ever fails to ship, the .app must still build and must NOT
# advertise an icon file that isn't there.
with tempfile.TemporaryDirectory(prefix="lunchbot-test-") as tmp:
    real = AB.ICON_SOURCE
    AB.ICON_SOURCE = Path(tmp) / "definitely-absent.icns"
    try:
        app = AB.install_app(Path(tmp))
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        check((app / "Contents" / "MacOS" / AB.APP_NAME).is_file(),
              "a missing icns still produces a launchable .app")
        check("CFBundleIconFile" not in info,
              "a missing icns omits CFBundleIconFile (no dangling reference)")
    finally:
        AB.ICON_SOURCE = real

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all tests passed")
