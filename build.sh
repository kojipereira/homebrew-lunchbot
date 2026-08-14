#!/bin/sh
# Produce the two release artifacts:
#
#   dist/lunchbot-<VERSION>.tar.gz            pip-installable source (the formula)
#   dist/Lunchbot-Installer-<VERSION>.tar.gz  double-click installer (Releases page)
#
# Both must be attached to every GitHub release — the formula breaks without the
# first, and the no-terminal install path breaks without the second.
#
#   ./build.sh              # build at the version in ./VERSION
#   ./build.sh 1.2.3        # build at an explicit version (used to backfill an
#                           # older tag whose tree predates its own version bump)
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:-$(cat "$ROOT/VERSION")}"
STAGE_ROOT="$(mktemp -d)"
STAGE="$STAGE_ROOT/lunchbot-$VERSION"
OUT="$ROOT/dist"

# shasum on macOS, sha256sum on the Linux CI runner.
sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

# In-place sed without the -i flag (BSD and GNU disagree about its syntax).
rewrite() {
  sed "$1" "$2" > "$2.tmp" && mv "$2.tmp" "$2"
}

mkdir -p "$STAGE" "$OUT"
# pyproject.toml makes the tarball pip-installable (Homebrew formula uses it);
# install.sh/uninstall.sh remain the non-Homebrew fallback path.
cp "$ROOT/install.sh" "$ROOT/uninstall.sh" "$ROOT/README.md" "$ROOT/VERSION" \
   "$ROOT/pyproject.toml" "$STAGE/"
cp -R "$ROOT/src" "$ROOT/libexec" "$ROOT/templates" "$STAGE/"

# Stamp the requested version through the staged copy, so the tarball is
# self-consistent even when it differs from the working tree's VERSION.
printf '%s\n' "$VERSION" > "$STAGE/VERSION"
rewrite "s/^version = \".*\"\$/version = \"$VERSION\"/" "$STAGE/pyproject.toml"
rewrite "s/^__version__ = \".*\"\$/__version__ = \"$VERSION\"/" \
        "$STAGE/src/lunchbot/__init__.py"

# Strip caches.
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true

TARBALL="$OUT/lunchbot-$VERSION.tar.gz"
tar -C "$STAGE_ROOT" -czf "$TARBALL" "lunchbot-$VERSION"
rm -rf "$STAGE_ROOT"

SHA="$(sha256 "$TARBALL")"
echo "built $TARBALL"
echo "sha256: $SHA"

# ---- Friendly installer bundle (download → expand → double-click) -----------
# A separate, human-facing tar.gz for the Releases page. Expands to a clearly
# named folder holding double-clickable .command files + a Read Me.
INST_ROOT="$(mktemp -d)"
INST="$INST_ROOT/Lunchbot Installer"
mkdir -p "$INST"
cp "$ROOT/Install Lunchbot.command" "$ROOT/Uninstall Lunchbot.command" "$INST/"
chmod +x "$INST/Install Lunchbot.command" "$INST/Uninstall Lunchbot.command"
cat > "$INST/Read Me First.txt" <<EOF
Lunchbot — auto-order your weekday lunch on DoorDash

TO INSTALL
  1. Double-click "Install Lunchbot.command".
     If macOS says it can't be opened, right-click it → Open → Open.
  2. Follow the prompts in the Terminal window that opens.

It installs Lunchbot (via Homebrew), adds a bot icon to your menu bar,
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
tar -C "$INST_ROOT" -czf "$INSTALLER" "Lunchbot Installer"
rm -rf "$INST_ROOT"
echo "built $INSTALLER  (the double-click installer for the Releases page)"

# Fail loudly rather than let a release go out half-built.
for f in "$TARBALL" "$INSTALLER"; do
  [ -s "$f" ] || { echo "ERROR: $f is missing or empty" >&2; exit 1; }
done

echo
echo "Both assets are in $OUT. ./release.sh uploads them and verifies they"
echo "landed; .github/workflows/release-assets.yml backstops any release that"
echo "still ends up missing one."
