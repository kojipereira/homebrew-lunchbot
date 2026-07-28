#!/bin/sh
# Double-clickable uninstaller. Removes the app, menu-bar agent, and schedule.
# Leaves your config/history in place.
set -u

printf '\n  🥪  Uninstalling Lunchbot…\n\n'

lunchbot uninstall-agent 2>/dev/null || true
lunchbot uninstall-gui-agent 2>/dev/null || true
lunchbot uninstall-app 2>/dev/null || true
# Forget that this version was provisioned, so a reinstall re-creates the app
# bundle and the menu-bar agent instead of assuming they're still there.
rm -f "${XDG_STATE_HOME:-$HOME/.local/state}/lunchbot/bootstrap.json"
if command -v brew >/dev/null 2>&1; then
  brew uninstall lunchbot 2>/dev/null || true
fi

printf '  ✅  Removed the menu-bar app, its icon, and the daily schedule.\n\n'
echo "  Your settings and order history under ~/.config/lunchbot and"
echo "  ~/.local/state/lunchbot were left in place — delete them by hand if"
echo "  you want a completely clean slate."
printf '\n  Press Return to close this window.'; read -r _
