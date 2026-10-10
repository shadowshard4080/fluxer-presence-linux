#!/bin/sh
# Removes the fluxer-presence systemd user service. Leaves config.json alone.
set -eu

UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/fluxer-presence.service"

systemctl --user disable --now fluxer-presence 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
echo "Removed fluxer-presence service. Your status was restored when it stopped."
