#!/bin/sh
# One-command release: bump version, build both tarballs, cut a GitHub release
# with BOTH assets attached, patch the formula's url+sha256, and open a PR
# (main is protected).
#
#   ./release.sh 1.1.6                  # full release
#   ./release.sh 1.1.4 --assets-only    # repair an existing release that is
#                                       # missing one of its two assets
#
# Requires: gh (authenticated), a clean-ish working tree.
#
# Two things this script is careful about, because both have bitten us:
#
#   * The tag is created on the release branch, NOT on main. Cutting it on main
#     before the bump PR merged is what left v1.1.4 and v1.1.5 tagging trees
#     that still said 1.1.2 and 1.1.3.
#   * Every release is verified to carry both assets before the script exits 0.
#     A release with only lunchbot-<ver>.tar.gz silently breaks the
#     download-and-double-click install path.
set -eu

VER="${1:-}"
MODE="${2:-full}"
case "$VER" in
  "" ) echo "usage: ./release.sh X.Y.Z [--assets-only]" >&2; exit 1 ;;
  v* ) VER="${VER#v}" ;;
esac
REPO="kojipereira/homebrew-lunchbot"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

TARBALL="dist/lunchbot-$VER.tar.gz"
INSTALLER="dist/Lunchbot-Installer-$VER.tar.gz"

# Whatever python3 is around — never a hardcoded Homebrew path.
PY="$(command -v python3.13 || command -v python3)" || {
  echo "python3 not found" >&2; exit 1
}

release_asset_names() {
  gh release view "v$VER" --repo "$REPO" --json assets --jq '.assets[].name' 2>/dev/null
}

has_asset() {
  release_asset_names | grep -Fqx "$1"
}

# Upload only the assets the release is missing. Never --clobber: the formula
# pins the sha256 of lunchbot-<ver>.tar.gz and a rebuild isn't byte-identical,
# so replacing a published source tarball would break `brew install` for people
# who already have the old checksum.
upload_missing_assets() {
  for f in "$TARBALL" "$INSTALLER"; do
    name="$(basename "$f")"
    if has_asset "$name"; then
      echo "  · $name already attached — leaving it alone"
    else
      echo "  · uploading $name"
      gh release upload "v$VER" "$f" --repo "$REPO"
    fi
  done
}

verify_assets() {
  missing=""
  for f in "$TARBALL" "$INSTALLER"; do
    name="$(basename "$f")"
    has_asset "$name" || missing="$missing $name"
  done
  if [ -n "$missing" ]; then
    echo >&2
    echo "ERROR: release v$VER is missing:$missing" >&2
    echo "Re-run: ./release.sh $VER --assets-only" >&2
    exit 1
  fi
  echo "verified: v$VER carries both assets"
}

# ---- --assets-only: repair an existing release ------------------------------
if [ "$MODE" = "--assets-only" ]; then
  echo "── repairing assets on v$VER ──"
  gh release view "v$VER" --repo "$REPO" >/dev/null || {
    echo "no release v$VER to repair" >&2; exit 1
  }
  sh build.sh "$VER" >/dev/null
  upload_missing_assets
  verify_assets
  exit 0
fi

echo "── releasing v$VER ──"

# 1. Bump version in the three places that track it.
"$PY" - "$VER" <<'PY'
import re, sys, pathlib
ver = sys.argv[1]
pathlib.Path("VERSION").write_text(ver + "\n")
p = pathlib.Path("pyproject.toml"); t = p.read_text()
p.write_text(re.sub(r'(?m)^version = ".*"$', f'version = "{ver}"', t))
p = pathlib.Path("src/lunchbot/__init__.py"); t = p.read_text()
p.write_text(re.sub(r'__version__ = ".*"', f'__version__ = "{ver}"', t))
print(f"bumped VERSION / pyproject / __init__ to {ver}")
PY

# 2. Build both tarballs and capture the source sha256.
sh build.sh >/dev/null
SHA="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
echo "built $TARBALL  sha256=$SHA"

# 3. Patch the formula url + sha256 before committing, so the tagged tree is the
#    tree the release actually describes.
"$PY" - "$VER" "$SHA" "$REPO" <<'PY'
import re, sys, pathlib
ver, sha, repo = sys.argv[1], sys.argv[2], sys.argv[3]
f = pathlib.Path("Formula/lunchbot.rb"); t = f.read_text()
url = f"https://github.com/{repo}/releases/download/v{ver}/lunchbot-{ver}.tar.gz"
t = re.sub(r'(?m)^  url ".*"$', f'  url "{url}"', t)
t = re.sub(r'(?m)^  sha256 ".*"$', f'  sha256 "{sha}"', t, count=1)
f.write_text(t)
print("patched formula url + sha256")
PY

# 4. Commit the bump on a release branch and push it.
BR="release-v$VER"
git checkout -B "$BR"
git add VERSION pyproject.toml src/lunchbot/__init__.py Formula/lunchbot.rb
git commit -q -m "Release v$VER"
git push -u origin "$BR"

# 5. Cut the release, tagging that branch commit (see the note at the top), with
#    both assets: the pip-installable source tarball for Homebrew and the
#    friendly double-click installer bundle for everyone else.
gh release create "v$VER" "$TARBALL" "$INSTALLER" --repo "$REPO" --target "$BR" \
  --title "lunchbot $VER" \
  --notes "Install: brew install kojipereira/lunchbot/lunchbot — or download **Lunchbot-Installer-$VER.tar.gz** below, expand it, and double-click \"Install Lunchbot.command\". Requires dd-cli (the installer explains how to get it)." >/dev/null
echo "created release v$VER (tagged $BR)"

# 6. Never exit 0 on a half-populated release.
upload_missing_assets
verify_assets

# 7. Open the PR (direct push to main is blocked).
gh pr create --repo "$REPO" --base main --head "$BR" \
  --title "Release v$VER" --body "Automated release. Merge to publish the formula update."

echo
echo "Done. Review + merge the PR, then users get it with:  brew upgrade lunchbot"
echo "Remember to refresh pyobjc/rumps pins if they changed:"
echo "  brew update-python-resources ./Formula/lunchbot.rb"
