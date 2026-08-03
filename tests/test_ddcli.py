"""DoorDash CLI error classification tests. Run with:
    PYTHONPATH=src python3.13 tests/test_ddcli.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot import ddcli  # noqa: E402


failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")
    else:
        print(f"ok:   {msg}")


try:
    ddcli._raise_command_error(1, "saved access token may be expired; run dd-cli login")
except ddcli.NotLoggedIn:
    check(True, "expired credentials become a recoverable sign-in error")
except Exception as e:  # noqa: BLE001
    check(False, f"expired credentials are classified correctly ({e})")
else:
    check(False, "expired credentials raise an error")

try:
    ddcli._raise_command_error(1, "SSL certificate verify failed")
except ddcli.TlsError:
    check(True, "certificate failures provide a TLS-specific recovery path")
except Exception as e:  # noqa: BLE001
    check(False, f"certificate failures are classified correctly ({e})")
else:
    check(False, "certificate failures raise an error")

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nall tests passed")
