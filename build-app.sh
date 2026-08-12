#!/bin/sh
# Build the self-contained Lunchbot.app and the drag-to-Applications DMG.
#
#   ./build-app.sh              # build at the version in ./VERSION
#   ./build-app.sh 1.2.3        # build at an explicit version
#
# Output:
#   dist/Lunchbot.app           the bundle (self-contained; no Homebrew needed)
#   dist/Lunchbot-<VERSION>.dmg the thing users download and drag
#
# This is a *different* distribution channel from build.sh, which produces the
# pip-installable tarball the Homebrew formula consumes. Both still ship: brew
# users are already installed and their path keeps working untouched.
#
# The bundle embeds a relocatable CPython from python-build-standalone rather
# than reusing the machine's. That is the whole point — a dragged .app cannot
# depend on /opt/homebrew existing on the machine it lands on.
#
# WHY NOT py2app: lunchbot needs two entry points out of one bundle, the
# menu-bar GUI *and* the `lunchbot run` CLI that launchd fires at lunchtime.
# py2app builds a single main executable over a zipped stdlib, and bolting a
# second command onto that fights the tool the whole way. An embedded
# interpreter makes both entry points three-line shell shims.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:-$(cat "$ROOT/VERSION")}"
OUT="$ROOT/dist"
CACHE="$ROOT/.cache"
APP="$OUT/Lunchbot.app"
C="$APP/Contents"

# --- pins --------------------------------------------------------------------
# CPython 3.13 to match the Homebrew formula's python@3.13, so the bundled and
# brewed builds run identical bytecode. Bump deliberately, never automatically:
# this is the runtime every user gets.
PY_RELEASE="20260807"
PY_VERSION="3.13.15"
PY_DIRNAME="python3.13"
PY_ASSET="cpython-${PY_VERSION}+${PY_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/${PY_ASSET}"

# Pinned to the same versions the formula resolves, so a bug reproduces the same
# way on both channels. pyobjc in particular ships often; an unpinned build
# would mean no two releases share a GUI stack.
DEPS="rumps==0.4.0 pyobjc-core==12.2.1 pyobjc-framework-Cocoa==12.2.1"

# Ad-hoc ("-") unless a real Developer ID is supplied. Signing is what makes
# notarization possible later; until then this at least gives the bundle one
# coherent signature instead of a pile of separately-signed dylibs, which is
# its own source of launch failures on Apple Silicon.
SIGN_IDENTITY="${LUNCHBOT_SIGN_IDENTITY:--}"

# Contents/MacOS/<these>. Must match bundle.APP_EXE / GUI_EXE / CLI_EXE.
# No two may differ by case alone — macOS is case-insensitive and the second
# write would silently replace the first. See the shim section below.
APP_EXE="Lunchbot"
GUI_EXE="lunchbot-gui"
CLI_EXE="lunchbot-cli"

echo "── building Lunchbot.app $VERSION ──"

# --- 1. fetch the interpreter (cached; it is 24 MB and never changes) --------
mkdir -p "$CACHE" "$OUT"
if [ ! -f "$CACHE/$PY_ASSET" ]; then
  echo "downloading $PY_ASSET"
  curl -fsSL --retry 3 -o "$CACHE/$PY_ASSET.part" "$PY_URL"
  mv "$CACHE/$PY_ASSET.part" "$CACHE/$PY_ASSET"
else
  echo "using cached $PY_ASSET"
fi

# --- 2. lay out the bundle ---------------------------------------------------
# The interpreter goes in Contents/Resources, NOT Contents/Frameworks. codesign
# applies framework-layout rules to everything under Frameworks and rejects a
# CPython tree outright ("bundle format unrecognized, invalid, or unsuitable"),
# because lib/python3.13/ is not a versioned framework. Resources has no such
# rule and the nested Mach-O still gets signed individually below.
rm -rf "$APP"
mkdir -p "$C/MacOS" "$C/Resources"
tar -xzf "$CACHE/$PY_ASSET" -C "$C/Resources"
PYHOME="$C/Resources/python"
PY="$PYHOME/bin/python3"
SP="$PYHOME/lib/$PY_DIRNAME/site-packages"

echo "installing deps"
# shellcheck disable=SC2086 # DEPS is a deliberate word-split list of pins.
"$PY" -m pip install --quiet --disable-pip-version-check --no-input $DEPS

# The app itself goes in beside its deps, so both entry points import it with
# no PYTHONPATH games (and `-s` below can stay on).
cp -R "$ROOT/src/lunchbot" "$SP/lunchbot"

# Stamp the version through the copy that ships, leaving the working tree alone.
sed "s/^__version__ = \".*\"\$/__version__ = \"$VERSION\"/" \
    "$SP/lunchbot/__init__.py" > "$SP/lunchbot/__init__.py.tmp"
mv "$SP/lunchbot/__init__.py.tmp" "$SP/lunchbot/__init__.py"

# --- 3. prune ----------------------------------------------------------------
# 105 MB → ~60 MB. Tk is the big one and it is pure dead weight: the
# preferences window has been plain AppKit since the Tk dependency was dropped.
# PyObjCTest is 16 MB of upstream's own test suite.
echo "pruning"
PYLIB="$PYHOME/lib"
rm -rf "$SP/PyObjCTest" \
       "$SP/pip" "$SP/setuptools" "$SP/pkg_resources" \
       "$SP"/pip-*.dist-info "$SP"/setuptools-*.dist-info
rm -rf "$PYLIB/$PY_DIRNAME/test" \
       "$PYLIB/$PY_DIRNAME/idlelib" \
       "$PYLIB/$PY_DIRNAME/tkinter" \
       "$PYLIB/$PY_DIRNAME/ensurepip" \
       "$PYLIB/$PY_DIRNAME/pydoc_data" \
       "$PYLIB/$PY_DIRNAME/turtledemo" \
       "$PYLIB/tcl9" "$PYLIB/tcl9.0" "$PYLIB/tk9.0" \
       "$PYLIB/itcl4.3.8" "$PYLIB/thread3.0.6" "$PYLIB/pkgconfig" \
       "$PYHOME/include" "$PYHOME/share"
rm -f  "$PYLIB/libtcl9.0.dylib" "$PYLIB/libtcl9tk9.0.dylib" \
       "$PYLIB/$PY_DIRNAME/lib-dynload/_tkinter."*.so
find "$C" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$C" -name '*.pyc' -delete 2>/dev/null || true

# --- 4. entry points ---------------------------------------------------------
# Real .py launchers rather than `python3 -c '…'`: the shell shims stay
# quote-free and a traceback names a file someone can actually open.
cat > "$C/Resources/launch-gui.py" <<'EOF'
"""Bundle entry point for the menu-bar app (CFBundleExecutable)."""
import sys

from lunchbot.gui.app import main

sys.exit(main(sys.argv[1:]))
EOF

cat > "$C/Resources/launch-cli.py" <<'EOF'
"""Bundle entry point for the CLI — what the launchd ordering agent runs."""
import sys

from lunchbot.__main__ import main

sys.exit(main(sys.argv[1:]))
EOF

# Shared preamble: resolve the bundle from our own location, so the .app works
# from /Applications, ~/Applications, ~/Downloads or a mounted DMG.
#
# Walking symlinks matters: ~/.local/bin/lunchbot is a symlink to the CLI shim,
# and without this \$0 resolves to ~/.local/bin, putting the interpreter at
# ~/.local/Resources/python/bin/python3 — which is nothing at all. `readlink -f`
# would do it in one line but is not portable to older macOS.
RESOLVE='SELF="$0"
while [ -L "$SELF" ]; do
  LINK="$(readlink "$SELF")"
  case "$LINK" in
    /*) SELF="$LINK" ;;
    *)  SELF="$(dirname "$SELF")/$LINK" ;;
  esac
done
C="$(cd "$(dirname "$SELF")/.." && pwd)"'

# -s keeps the user's ~/.local/lib/python3.13/site-packages out of a shipped
# app: a stray local rumps or pyobjc there would silently take precedence over
# the pinned copies and break the menu bar on exactly one person's machine.

# 1. The menu bar itself. The login LaunchAgent points here, and nothing else
#    should — see APP_EXE below for why.
cat > "$C/MacOS/$GUI_EXE" <<EOF
#!/bin/sh
$RESOLVE
exec "\$C/Resources/python/bin/python3" -s "\$C/Resources/launch-gui.py" "\$@"
EOF

# 2. The CLI. The ordering LaunchAgent runs this as \`… run\`, and
#    ~/.local/bin/lunchbot symlinks to it.
cat > "$C/MacOS/$CLI_EXE" <<EOF
#!/bin/sh
$RESOLVE
exec "\$C/Resources/python/bin/python3" -s "\$C/Resources/launch-cli.py" "\$@"
EOF

# 3. CFBundleExecutable — what Finder, Launchpad, the Dock and \`open\` run.
#    It deliberately does NOT exec the menu bar. Started through LaunchServices
#    the process comes up fine and never paints its status item: the menu bar
#    just stays empty. Started by launchd the identical binary always renders.
#    So this asks launchd to run the app and then forwards the user's real
#    request ("show me Lunchbot") to that copy.
#
#    This is why GUI_EXE exists separately. If the LaunchAgent pointed at this
#    script, the kickstart below would restart the very job launchd had just
#    started, forever.
cat > "$C/MacOS/$APP_EXE" <<EOF
#!/bin/sh
$RESOLVE
AGENT="gui/\$(id -u)/com.lunchbot.gui"

# First launch after a drag: nothing has registered the login agent yet, and
# the CLI's bootstrap is the same provisioning path the Homebrew installer uses.
if ! launchctl print "\$AGENT" >/dev/null 2>&1; then
  "\$C/MacOS/$CLI_EXE" bootstrap >/dev/null 2>&1
fi

if launchctl print "\$AGENT" >/dev/null 2>&1; then
  launchctl kickstart -k "\$AGENT" >/dev/null 2>&1
  sleep 1
fi

# --prefs because opening the app from Finder means "show me Lunchbot". If the
# launchd copy won the singleton lock this invocation hands the request over and
# exits; if launchd was unavailable, it runs the menu bar itself rather than
# leaving the user with nothing.
exec "\$C/Resources/python/bin/python3" -s "\$C/Resources/launch-gui.py" --prefs
EOF

chmod +x "$C/MacOS/$GUI_EXE" "$C/MacOS/$CLI_EXE" "$C/MacOS/$APP_EXE"

# --- 5. Info.plist + icon ----------------------------------------------------
cp "$ROOT/src/lunchbot/resources/Lunchbot.icns" "$C/Resources/Lunchbot.icns"
"$PY" - "$C" "$VERSION" <<'PY'
import plistlib
import sys
from pathlib import Path

contents, version = Path(sys.argv[1]), sys.argv[2]
(contents / "Info.plist").write_bytes(plistlib.dumps({
    "CFBundleName": "Lunchbot",
    "CFBundleDisplayName": "Lunchbot",
    "CFBundleIdentifier": "com.lunchbot.app",
    "CFBundleExecutable": "Lunchbot",
    "CFBundleIconFile": "Lunchbot.icns",
    "CFBundlePackageType": "APPL",
    "CFBundleVersion": version,
    "CFBundleShortVersionString": version,
    # Menu-bar accessory: no Dock icon while running, still double-clickable.
    "LSUIElement": True,
    "LSMinimumSystemVersion": "11.0",
}))
PY

# --- 6. sign -----------------------------------------------------------------
# Inner Mach-O first, bundle last: signing the bundle seals what is inside it,
# so anything signed afterwards invalidates the outer signature.
echo "signing (identity: $SIGN_IDENTITY)"
find "$PYHOME" \( -name '*.dylib' -o -name '*.so' -o -name 'python3.*' \) -type f -print0 |
  xargs -0 -n1 codesign --force --timestamp=none --sign "$SIGN_IDENTITY" 2>/dev/null || true

# Only CFBundleExecutable is covered by the bundle seal. The other two shims in
# Contents/MacOS are nested code and need their own signatures first, or sealing
# fails with "code object is not signed at all".
codesign --force --timestamp=none --sign "$SIGN_IDENTITY" "$C/MacOS/$CLI_EXE"
codesign --force --timestamp=none --sign "$SIGN_IDENTITY" "$C/MacOS/$GUI_EXE"

codesign --force --timestamp=none --sign "$SIGN_IDENTITY" "$APP" || {
  echo "  ERROR: could not sign the bundle." >&2
  exit 1
}
codesign --verify --strict "$APP" &&
  echo "  signature verifies" ||
  { echo "  ERROR: signature does not verify" >&2; exit 1; }

# --- 7. DMG ------------------------------------------------------------------
# Staging dir with the app beside a symlink to /Applications — the drag-here
# window every Mac user already knows.
DMG="$OUT/Lunchbot-$VERSION.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/Lunchbot.app"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "Lunchbot $VERSION" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG"
rm -rf "$STAGE"

[ -s "$DMG" ] || { echo "ERROR: $DMG is missing or empty" >&2; exit 1; }

echo
echo "built $APP"
echo "      $(du -sh "$APP" | awk '{print $1}') on disk"
echo "built $DMG"
echo "      $(du -sh "$DMG" | awk '{print $1}') compressed"
echo
if [ "$SIGN_IDENTITY" = "-" ]; then
  echo "NOTE: this build is ad-hoc signed, not notarized. Downloaded from a"
  echo "browser it will be quarantined, and macOS 15+ sends the user through"
  echo "System Settings > Privacy & Security to approve it. To ship without"
  echo "that, set LUNCHBOT_SIGN_IDENTITY to a Developer ID Application cert"
  echo "and notarize the DMG with notarytool."
fi
