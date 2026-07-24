#!/bin/sh
# Print an absolute path to a Python >= 3.11 interpreter, or fail.
# Prefer the stable versioned Homebrew symlink (survives `brew upgrade python@3.13`
# patch bumps); never the Cellar path, never bare python3 (can jump major versions).
for cand in \
  /opt/homebrew/bin/python3.13 \
  /opt/homebrew/bin/python3.12 \
  /opt/homebrew/bin/python3.11 \
  /opt/homebrew/opt/python@3.13/bin/python3.13 \
  python3.13 python3.12 python3.11 python3
do
  p="$(command -v "$cand" 2>/dev/null)" || continue
  if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    printf '%s\n' "$p"
    exit 0
  fi
done
echo "lunchbot: no Python >= 3.11 found. Install one with: brew install python@3.13" >&2
exit 1
