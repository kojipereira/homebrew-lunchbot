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
echo
echo "To publish via Homebrew:"
echo "  1. Upload $TARBALL as a GitHub Release asset (tag v$VERSION)."
echo "  2. In Formula/lunchbot.rb set url to that asset and sha256 to the value above."
echo "  3. Run: brew update-python-resources ./Formula/lunchbot.rb   (fills rumps/pyobjc)."
echo "  4. Commit the formula to your homebrew-lunchbot tap repo."
echo
echo "Non-Homebrew fallback:  tar xzf lunchbot-$VERSION.tar.gz && cd lunchbot-$VERSION && ./install.sh"
