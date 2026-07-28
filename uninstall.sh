#!/bin/sh
# Remove lunchbot. Keeps config + state unless --purge is given.
set -eu

DATA="$HOME/.local/share/lunchbot"
LA="$HOME/Library/LaunchAgents"

echo "── lunchbot uninstall ──"

# Tear down both agents (and any legacy labels), directly — don't rely on the
# launcher still existing.
for LABEL in com.lunchbot.agent com.lunchbot.gui com.koji.lunchbot; do
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  [ -f "$LA/$LABEL.plist" ] && rm -f "$LA/$LABEL.plist" && echo "removed $LABEL.plist"
done

rm -f "$HOME/.local/bin/lunchbot" "$HOME/.local/bin/lunchbot-gui" && echo "removed launchers"
rm -rf "$DATA" && echo "removed $DATA"
rm -rf "$HOME/Applications/Lunchbot.app" 2>/dev/null && echo "removed Lunchbot.app" || true
# Forget that this version was provisioned, so a reinstall re-creates the app
# bundle and the menu-bar agent instead of assuming they're still there.
rm -f "${XDG_STATE_HOME:-$HOME/.local/state}/lunchbot/bootstrap.json"

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/.config/lunchbot" "$HOME/.local/state/lunchbot"
  rm -f "$HOME/Library/Logs/lunchbot.log"* \
        "$HOME/Library/Logs/lunchbot.stdout.log" \
        "$HOME/Library/Logs/lunchbot.stderr.log"
  echo "purged config, state, and logs"
else
  echo "kept config (~/.config/lunchbot) and state. Use --purge to remove them."
fi
