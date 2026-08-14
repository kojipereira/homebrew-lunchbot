"""Self-contained Lunchbot.app detection and the paths that hang off it.

    PYTHONPATH=src python3.13 tests/test_bundle.py

Exits non-zero on failure. Everything runs against fabricated bundles in a temp
dir — it never reads ~/Applications, never writes ~/.local/bin, and never calls
launchctl, so it is safe on a machine with a live lunchbot install.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lunchbot import agent as A          # noqa: E402
from lunchbot import appbundle as AB     # noqa: E402
from lunchbot import bundle as B         # noqa: E402

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")
    else:
        print(f"ok:   {msg}")


def make_bundle(root: Path, name="Lunchbot.app", embedded=True) -> Path:
    """A fake .app. `embedded=False` produces the Homebrew-style shell stub."""
    app = root / name
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    # Write each shim with its own name as the body, so a case-collision
    # between them is visible as content rather than silently surviving.
    for exe in (B.APP_EXE, B.GUI_EXE, B.CLI_EXE):
        (macos / exe).write_text(f"#!/bin/sh\n# {exe}\n")
    if embedded:
        py = app / "Contents" / "Resources" / "python" / "bin"
        py.mkdir(parents=True, exist_ok=True)
        (py / "python3").write_text("#!/bin/sh\n")
    return app


# --- the three executables must not collide on a case-insensitive filesystem --
# macOS is case-insensitive by default, so Contents/MacOS/lunchbot and
# Contents/MacOS/Lunchbot are one file and the second write wins. That shipped:
# the app built, signed and launched, and double-clicking it ran the setup
# wizard in a terminal instead of putting the bot icon in the menu bar.
EXES = (B.APP_EXE, B.GUI_EXE, B.CLI_EXE)
check(len({e.lower() for e in EXES}) == len(EXES),
      f"the three bundle executables {EXES} differ by more than case")
check(B.APP_EXE == "Lunchbot", "APP_EXE matches CFBundleExecutable")

# The login agent must target the real menu-bar entry point, never
# CFBundleExecutable — that one kickstarts the login agent, so pointing launchd
# at it would make the job restart itself forever.
check(B.GUI_EXE != B.APP_EXE,
      "the login agent's target is distinct from CFBundleExecutable")

# And prove the no-collision property on the filesystem this test is running on,
# rather than trusting the string comparison above to model it.
with tempfile.TemporaryDirectory(prefix="lunchbot-test-") as tmp:
    macos = Path(tmp) / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    for exe in EXES:
        (macos / exe).write_text(exe)
    for exe in EXES:
        check((macos / exe).read_text() == exe,
              f"{exe} survived writing the other shims on this filesystem")
    check(len(list(macos.iterdir())) == len(EXES),
          f"all {len(EXES)} shims exist as distinct files")


# --- what counts as a bundle -------------------------------------------------
# The distinction that matters: Homebrew also installs a Lunchbot.app, but it is
# a stub that execs the brew-installed lunchbot-gui. Pointing a LaunchAgent at
# that stub would send launcher resolution straight back through itself.
with tempfile.TemporaryDirectory(prefix="lunchbot-test-") as tmp:
    root = Path(tmp)
    real = make_bundle(root / "real", embedded=True)
    stub = make_bundle(root / "stub", embedded=False)

    check(B._is_bundle(real), "a bundle carrying its own interpreter is a bundle")
    check(not B._is_bundle(stub), "the Homebrew shell stub is NOT treated as a bundle")
    check(not B._is_bundle(root / "nope.app"), "a path that doesn't exist is not a bundle")

    # --- running_bundle walks up from sys.executable -------------------------
    saved = sys.executable
    try:
        sys.executable = str(real / "Contents" / "Resources" / "python" / "bin" / "python3")
        # .resolve() on both sides: running_bundle resolves symlinks on purpose
        # (that is how an App Translocation path becomes visible), and macOS
        # hands out temp dirs under /var/folders, itself a symlink.
        check(B.running_bundle() == real.resolve(),
              "running_bundle finds the .app we execute from")
        check(B.executable(B.CLI_EXE, real) == real / "Contents" / "MacOS" / B.CLI_EXE,
              "executable() resolves Contents/MacOS/<name>")
        check(B.executable("absent", real) is None, "a missing executable resolves to None")

        sys.executable = str(stub / "Contents" / "MacOS" / "Lunchbot")
        check(B.running_bundle() is None, "running from the stub reports no bundle")

        sys.executable = "/opt/homebrew/opt/python@3.13/bin/python3.13"
        check(B.running_bundle() is None, "a Homebrew interpreter reports no bundle")
    finally:
        sys.executable = saved

# --- ephemeral locations -----------------------------------------------------
# Both of these are routine, and both leave launchd holding a path that is gone
# by the time lunch comes round.
check(B.is_ephemeral(Path("/Volumes/Lunchbot 1.1.9/Lunchbot.app")),
      "an app on a mounted DMG is ephemeral")
check(B.is_ephemeral(Path("/private/var/folders/x9/AppTranslocation/ABC/d/Lunchbot.app")),
      "an App Translocation path is ephemeral")
check(not B.is_ephemeral(Path("/Applications/Lunchbot.app")),
      "/Applications is not ephemeral")
check(not B.is_ephemeral(None), "no bundle at all is not ephemeral")

# --- the launcher chain prefers the bundle -----------------------------------
with tempfile.TemporaryDirectory(prefix="lunchbot-test-") as tmp:
    app = make_bundle(Path(tmp))
    saved = B.installed_bundle
    try:
        B.installed_bundle = lambda: app
        check(A._resolve_launcher("lunchbot", B.CLI_EXE)
              == app / "Contents" / "MacOS" / B.CLI_EXE,
              "the CLI launcher resolves into the bundle when one is installed")
        check(A._resolve_launcher("lunchbot-gui", B.GUI_EXE)
              == app / "Contents" / "MacOS" / B.GUI_EXE,
              "the GUI launcher resolves to the menu-bar entry point, not CFBundleExecutable")

        B.installed_bundle = lambda: None
        fallback = A._resolve_launcher("lunchbot-gui", B.GUI_EXE)
        check("Lunchbot.app" not in str(fallback),
              "with no bundle the resolver falls back to the Homebrew/dev path")
    finally:
        B.installed_bundle = saved

# --- install_app must not create a second app beside a real bundle -----------
# Left unguarded, bootstrap would write a Homebrew-style stub into
# ~/Applications while the user is running the real thing from /Applications,
# and the stub only knows how to look for a brew install that may not exist.
with tempfile.TemporaryDirectory(prefix="lunchbot-test-") as tmp:
    app = make_bundle(Path(tmp))
    saved_running, saved_shim = B.running_bundle, B.install_cli_shim
    shim_calls = []
    try:
        B.running_bundle = lambda: app
        B.install_cli_shim = lambda: shim_calls.append(1)
        check(AB.install_app() == app, "install_app returns the running bundle unchanged")
        check(shim_calls == [1], "install_app links the terminal shim instead")
        check(not (AB.DEFAULT_APP_DIR / "Lunchbot.app").is_relative_to(Path(tmp)),
              "install_app wrote nothing into the default app dir")
    finally:
        B.running_bundle, B.install_cli_shim = saved_running, saved_shim

# --- the shim never clobbers a real file -------------------------------------
# ~/.local/bin/lunchbot is install.sh's launcher on the non-Homebrew path.
# Replacing someone's working install with a symlink is not ours to do.
with tempfile.TemporaryDirectory(prefix="lunchbot-test-") as tmp:
    app = make_bundle(Path(tmp))
    saved_dir, saved_path, saved_running = B.SHIM_DIR, B.SHIM_PATH, B.running_bundle
    try:
        B.SHIM_DIR = Path(tmp) / "bin"
        B.SHIM_PATH = B.SHIM_DIR / "lunchbot"
        B.running_bundle = lambda: app

        check(B.install_cli_shim() == B.SHIM_PATH, "install_cli_shim creates the symlink")
        check(B.SHIM_PATH.resolve() == (app / "Contents" / "MacOS" / B.CLI_EXE).resolve(),
              "the symlink points at the bundle's CLI")
        check(B.install_cli_shim() == B.SHIM_PATH, "install_cli_shim is idempotent")

        B.SHIM_PATH.unlink()
        B.SHIM_PATH.write_text("#!/bin/sh\n# install.sh's launcher\n")
        check(B.install_cli_shim() is None, "a real file at the shim path is left alone")
        check(B.SHIM_PATH.read_text().endswith("launcher\n"),
              "…and its contents are untouched")
    finally:
        B.SHIM_DIR, B.SHIM_PATH, B.running_bundle = saved_dir, saved_path, saved_running

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all tests passed")
