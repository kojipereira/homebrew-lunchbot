#!/bin/sh
# lunchbot installer — stdlib-only, no pip/venv.
# Drops the package under ~/.local/share/lunchbot, writes a launcher at
# ~/.local/bin/lunchbot, then runs the setup wizard.
set -eu

BUNDLE="$(cd "$(dirname "$0")" && pwd)"
DATA="$HOME/.local/share/lunchbot"
LIB="$DATA/lib"
LIBEXEC="$DATA/libexec"
BIN="$HOME/.local/bin"
LAUNCHER="$BIN/lunchbot"

echo "── lunchbot install ──"

# 1. Apple Silicon gate (dd-cli ships arm64-only). Use sysctl, NOT uname -m
#    (uname reports x86_64 under a Rosetta shell and would give a false negative).
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]; then
  echo "ERROR: lunchbot requires an Apple Silicon Mac (dd-cli is arm64-only)." >&2
  exit 1
fi

# 2. Interpreter check up front so we fail with a clear message before copying.
if ! PY="$(sh "$BUNDLE/libexec/resolve-python.sh")"; then
  exit 1
fi
echo "Using Python: $PY"

# 3. Install files.
rm -rf "$LIB" "$LIBEXEC"
mkdir -p "$LIB" "$LIBEXEC" "$BIN"
cp -R "$BUNDLE/src/lunchbot" "$LIB/lunchbot"
cp "$BUNDLE/libexec/resolve-python.sh" "$LIBEXEC/resolve-python.sh"
chmod +x "$LIBEXEC/resolve-python.sh"

cat > "$LAUNCHER" <<'EOF'
#!/bin/sh
LIB="$HOME/.local/share/lunchbot/lib"
PY="$("$HOME/.local/share/lunchbot/libexec/resolve-python.sh")" || exit 1
export PYTHONPATH="$LIB"
exec "$PY" -m lunchbot "$@"
EOF
chmod +x "$LAUNCHER"
echo "Installed launcher: $LAUNCHER"

# 4. Carry forward legacy rotation state (original ~/lunchbot install) if present.
NEW_STATE="$HOME/.local/state/lunchbot/state.json"
if [ -f "$HOME/lunchbot/state.json" ] && [ ! -f "$NEW_STATE" ]; then
  mkdir -p "$(dirname "$NEW_STATE")"
  cp "$HOME/lunchbot/state.json" "$NEW_STATE"
  echo "Carried over rotation state from ~/lunchbot/state.json"
fi

# 5. PATH advice.
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo
     echo "NOTE: $BIN is not on your PATH. Add this to ~/.zshrc:"
     echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
     echo "…then open a new terminal (or run: export PATH=\"\$HOME/.local/bin:\$PATH\")." ;;
esac

# 6. Setup wizard (skip with --no-setup for CI/testing).
if [ "${1:-}" = "--no-setup" ]; then
  echo "Skipping setup. Run \`lunchbot setup\` when ready."
  exit 0
fi
echo
"$LAUNCHER" setup
