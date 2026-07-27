#!/bin/sh
# One-command release: bump version, build the tarball, cut a GitHub release,
# patch the formula's url+sha256, and open a PR (main is protected).
#
#   ./release.sh 1.1.1
#
# Requires: gh (authenticated), a clean-ish working tree.
set -eu

VER="${1:-}"
case "$VER" in
  "" ) echo "usage: ./release.sh X.Y.Z" >&2; exit 1 ;;
  v* ) VER="${VER#v}" ;;
esac
REPO="kojipereira/homebrew-lunchbot"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "── releasing v$VER ──"

# 1. Bump version in the three places that track it.
/opt/homebrew/bin/python3.13 - "$VER" <<'PY'
import re, sys, pathlib
ver = sys.argv[1]
pathlib.Path("VERSION").write_text(ver + "\n")
p = pathlib.Path("pyproject.toml"); t = p.read_text()
p.write_text(re.sub(r'(?m)^version = ".*"$', f'version = "{ver}"', t))
p = pathlib.Path("src/lunchbot/__init__.py"); t = p.read_text()
p.write_text(re.sub(r'__version__ = ".*"', f'__version__ = "{ver}"', t))
print(f"bumped VERSION / pyproject / __init__ to {ver}")
PY

# 2. Build the tarball and capture its sha256.
sh build.sh >/dev/null
TARBALL="dist/lunchbot-$VER.tar.gz"
SHA="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
echo "built $TARBALL  sha256=$SHA"

# 3. Cut the GitHub release (creates tag v$VER, uploads both assets: the
#    pip-installable source tarball for Homebrew, and the friendly double-click
#    installer bundle for non-technical users).
INSTALLER="dist/Lunchbot-Installer-$VER.tar.gz"
gh release create "v$VER" "$TARBALL" "$INSTALLER" --repo "$REPO" \
  --title "lunchbot $VER" \
  --notes "Install: brew install kojipereira/lunchbot/lunchbot — or download **Lunchbot-Installer-$VER.tar.gz** below, expand it, and double-click \"Install Lunchbot.command\". Requires dd-cli (the installer explains how to get it)." >/dev/null
echo "created release v$VER"

# 4. Patch the formula url + sha256.
/opt/homebrew/bin/python3.13 - "$VER" "$SHA" "$REPO" <<'PY'
import re, sys, pathlib
ver, sha, repo = sys.argv[1], sys.argv[2], sys.argv[3]
f = pathlib.Path("Formula/lunchbot.rb"); t = f.read_text()
url = f"https://github.com/{repo}/releases/download/v{ver}/lunchbot-{ver}.tar.gz"
t = re.sub(r'(?m)^  url ".*"$', f'  url "{url}"', t)
t = re.sub(r'(?m)^  sha256 ".*"$', f'  sha256 "{sha}"', t, count=1)
f.write_text(t)
print("patched formula url + sha256")
PY

# 5. Commit on a release branch + open a PR (direct push to main is blocked).
BR="release-v$VER"
git checkout -b "$BR"
git add VERSION pyproject.toml src/lunchbot/__init__.py Formula/lunchbot.rb
git commit -q -m "Release v$VER"
git push -u origin "$BR"
gh pr create --repo "$REPO" --base main --head "$BR" \
  --title "Release v$VER" --body "Automated release. Merge to publish the formula update."

echo
echo "Done. Review + merge the PR, then users get it with:  brew upgrade lunchbot"
echo "Remember to refresh pyobjc/rumps pins if they changed:"
echo "  brew update-python-resources ./Formula/lunchbot.rb"
