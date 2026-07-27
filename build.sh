#!/bin/sh
# Produce a shareable tarball: dist/lunchbot-<VERSION>.tar.gz
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(cat "$ROOT/VERSION")"
STAGE="$(mktemp -d)/lunchbot-$VERSION"
OUT="$ROOT/dist"

mkdir -p "$STAGE" "$OUT"
# pyproject.toml makes the tarball pip-installable (Homebrew formula uses it);
# install.sh/uninstall.sh remain the non-Homebrew fallback path.
cp "$ROOT/install.sh" "$ROOT/uninstall.sh" "$ROOT/README.md" "$ROOT/VERSION" \
   "$ROOT/pyproject.toml" "$STAGE/"
cp -R "$ROOT/src" "$ROOT/libexec" "$ROOT/templates" "$STAGE/"

# Strip caches.
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true

TARBALL="$OUT/lunchbot-$VERSION.tar.gz"
tar -C "$(dirname "$STAGE")" -czf "$TARBALL" "lunchbot-$VERSION"
rm -rf "$(dirname "$STAGE")"

SHA="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
echo "built $TARBALL"
echo "sha256: $SHA"

# ---- Friendly installer bundle (download → expand → double-click) -----------
# A separate, human-facing tar.gz for the Releases page. Expands to a clearly
# named folder holding double-clickable .command files + a Read Me.
INST="$(mktemp -d)/Lunchbot Installer"
mkdir -p "$INST"
cp "$ROOT/Install Lunchbot.command" "$ROOT/Uninstall Lunchbot.command" "$INST/"
chmod +x "$INST/Install Lunchbot.command" "$INST/Uninstall Lunchbot.command"
cat > "$INST/Read Me First.txt" <<EOF
Lunchbot — auto-order your weekday lunch on DoorDash

TO INSTALL
  1. Double-click "Install Lunchbot.command".
     If macOS says it can't be opened, right-click it → Open → Open.
  2. Follow the prompts in the Terminal window that opens.

It installs Lunchbot (via Homebrew), adds a sandwich icon to your menu bar,
and opens Preferences so you can pick your restaurants.

REQUIREMENTS
  • Apple Silicon Mac
  • Homebrew — https://brew.sh (the installer tells you if it's missing)
  • The DoorDash CLI "dd-cli" — the installer explains how to get it

TO REMOVE
  Double-click "Uninstall Lunchbot.command".

Version $VERSION
EOF
INSTALLER="$OUT/Lunchbot-Installer-$VERSION.tar.gz"
tar -C "$(dirname "$INST")" -czf "$INSTALLER" "Lunchbot Installer"
rm -rf "$(dirname "$INST")"
echo "built $INSTALLER  (the double-click installer for the Releases page)"

echo
echo "To publish via Homebrew:"
echo "  1. Upload $TARBALL as a GitHub Release asset (tag v$VERSION)."
echo "  2. In Formula/lunchbot.rb set url to that asset and sha256 to the value above."
echo "  3. Run: brew update-python-resources ./Formula/lunchbot.rb   (fills rumps/pyobjc)."
echo "  4. Commit the formula to your homebrew-lunchbot tap repo."
echo
echo "Also upload $INSTALLER to the release so non-technical users can"
echo "download → expand → double-click \"Install Lunchbot.command\"."
echo
echo "Non-Homebrew fallback:  tar xzf lunchbot-$VERSION.tar.gz && cd lunchbot-$VERSION && ./install.sh"
