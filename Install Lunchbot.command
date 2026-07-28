#!/bin/sh
# Double-clickable installer. Finder opens .command files in Terminal, so a
# non-technical user can just double-click this instead of typing brew commands.
# (First run of a downloaded copy may need: right-click -> Open, to clear
# Gatekeeper.)
set -u

TAP="kojipereira/lunchbot"
printf '\n  🥪  Installing Lunchbot…\n\n'

if ! command -v brew >/dev/null 2>&1; then
  echo "  Homebrew is required. Install it from https://brew.sh and run this again."
  printf '\n  Press Return to close.'; read -r _; exit 1
fi

# Install (or upgrade if already tapped).
if brew list lunchbot >/dev/null 2>&1; then
  brew upgrade "$TAP/lunchbot" || brew upgrade lunchbot || true
else
  brew install "$TAP/lunchbot" || {
    echo; echo "  Install failed. If the tap is private, ask the owner to make it public."
    printf '\n  Press Return to close.'; read -r _; exit 1
  }
fi

# Register the menu-bar app + a clickable app icon, then open preferences.
# (`bootstrap` is what every lunchbot command runs on its own after an install
# or upgrade; calling it here makes the double-click path explicit and ordered.)
lunchbot bootstrap || true

printf '\n  ✅  Installed.\n'
echo "     • A 🥪 icon is now in your menu bar (top-right)."
echo "     • Lunchbot.app is in ~/Applications (double-click to open)."
echo
echo "  You still need the DoorDash CLI (dd-cli) — run 'lunchbot doctor' for steps."
echo "  Opening Preferences so you can pick restaurants…"
lunchbot prefs >/dev/null 2>&1 &

printf '\n  Press Return to close this window.'; read -r _
