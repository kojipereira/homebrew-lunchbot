"""Thin wrapper around the `dd-cli` binary + environment preflight.

dd-cli is owned by another team and NOT bundled with lunchbot. Everything here
either shells out to it or checks that a colleague's machine can actually run
it (present, not Gatekeeper-quarantined, logged in, network/TLS sane).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

# lunchbot does not bundle, fetch, or redistribute dd-cli — each user brings
# their own. Point them here when it's missing. Set DDCLI_GET_URL via the
# LUNCHBOT_DDCLI_URL env var (or edit this default) for your distribution.
DDCLI_GET_URL = os.environ.get(
    "LUNCHBOT_DDCLI_URL",
    "https://github.com/your-org/dd-cli/releases (ask whoever shared lunchbot with you)",
)


def ddcli_get_instructions() -> str:
    """Human-readable steps for obtaining dd-cli (shown in doctor + the GUI)."""
    return (
        "lunchbot needs the DoorDash CLI (dd-cli), which you install yourself:\n"
        f"  1. Download dd-cli for Apple Silicon from:\n     {DDCLI_GET_URL}\n"
        "  2. Move it onto your PATH, e.g.:  mv ~/Downloads/dd-cli /opt/homebrew/bin/\n"
        "  3. If macOS blocks it (downloaded binary):  "
        "xattr -d com.apple.quarantine $(command -v dd-cli)\n"
        "  4. Sign in:  dd-cli login\n"
        "Then re-run lunchbot."
    )


DDCLI = "dd-cli"


class DdError(RuntimeError):
    pass


class NotLoggedIn(DdError):
    pass


class TlsError(DdError):
    pass


def which() -> str | None:
    return shutil.which(DDCLI)


def dd(*args: str, timeout: int = 60) -> dict:
    """Call dd-cli with --json-output and unwrap the {content:[{text}]} envelope
    (the text field is itself a JSON string that must be parsed again)."""
    cmd = [DDCLI, "--json-output", *args]
    logging.info("exec: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise DdError(f"dd-cli failed ({result.returncode}): {stderr}")
    return _unwrap(result.stdout)


def _unwrap(stdout: str) -> dict:
    outer = json.loads(stdout)
    content = outer.get("content") or []
    if not content or "text" not in content[0]:
        # Some commands may return the payload directly.
        return outer
    return json.loads(content[0]["text"])


def version() -> str | None:
    exe = which()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or out.stderr.strip() or None
    except Exception:
        return None


# ---- Gatekeeper / quarantine (bites colleagues who download the binary) -----
def is_quarantined(exe: str) -> bool:
    try:
        r = subprocess.run(["xattr", "-p", "com.apple.quarantine", exe],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def gatekeeper_status(exe: str) -> str:
    try:
        r = subprocess.run(["spctl", "-a", "-t", "execute", "-vv", exe],
                           capture_output=True, text=True, timeout=10)
        return (r.stderr or r.stdout or "").strip()
    except Exception as e:
        return f"spctl check failed: {e}"


def dequarantine_hint(exe: str) -> str:
    return f"xattr -d com.apple.quarantine {exe}"


# ---- Login probe (dd-cli has no whoami; probe an authed call) ---------------
def login_probe() -> None:
    """Raise NotLoggedIn / TlsError / DdError if an authed call doesn't work.
    Returns None on success."""
    try:
        dd("order", "history", timeout=45)
    except DdError as e:
        msg = str(e).lower()
        if any(k in msg for k in ("certificate", "ssl", "tls", "self-signed", "self signed", "ca ")):
            raise TlsError(
                "dd-cli hit a TLS/certificate error — likely a corporate proxy "
                "(Zscaler/Netskope). Set DD_CLI_CA_BUNDLE to your CA PEM."
            ) from e
        if any(k in msg for k in ("login", "auth", "unauthorized", "401", "token", "expired", "sign in")):
            raise NotLoggedIn("dd-cli is not logged in. Run: dd-cli login") from e
        # Ambiguous: treat as needing login, the most common cause.
        raise NotLoggedIn(f"dd-cli could not complete an authed call: {e}") from e


def login_interactive() -> bool:
    """Run the browser-based `dd-cli login`. Returns True on exit 0."""
    exe = which()
    if not exe:
        return False
    try:
        return subprocess.run([exe, "login"]).returncode == 0
    except Exception:
        return False


# ---- Address management -----------------------------------------------------
def list_addresses() -> list[dict]:
    """Return the user's saved delivery addresses."""
    resp = dd("address", "list")
    return resp.get("addresses", []) or []


def set_address(address_id: str) -> None:
    """Set the account-wide default delivery address."""
    dd("address", "set", "--address-id", address_id, "--yes")
