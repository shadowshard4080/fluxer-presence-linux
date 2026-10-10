#!/bin/sh
# Installs fluxer-presence as a systemd user service (Linux).
# Run from the repo folder: ./install.sh
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
PY=$(command -v python3 || true)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/fluxer-presence.service"

if [ -z "$PY" ]; then
    echo "python3 not found. Install Python 3.9+ with your package manager first." >&2
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    echo "Python 3.9+ is required (found $("$PY" --version))." >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd not found. Run it manually instead: $PY $DIR/fluxer_presence.py" >&2
    exit 1
fi

if [ ! -f "$DIR/config.json" ]; then
    cp "$DIR/config.example.json" "$DIR/config.json"
    echo "Created $DIR/config.json"
fi
chmod 600 "$DIR/config.json"

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Fluxer rich presence
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=$PY $DIR/fluxer_presence.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=graphical-session.target
EOF
echo "Installed $UNIT"

systemctl --user daemon-reload
systemctl --user enable fluxer-presence >/dev/null 2>&1

has_token=$("$PY" -c 'import json, os, sys
t = os.environ.get("FLUXER_TOKEN") or json.load(open(sys.argv[1])).get("token", "")
print("yes" if t.strip() else "no")' "$DIR/config.json")

if [ "$has_token" = yes ]; then
    systemctl --user restart fluxer-presence
    echo "Started. Logs: journalctl --user -u fluxer-presence -f"
else
    echo
    echo "Next: put your Fluxer token in $DIR/config.json (\"token\"), then run:"
    echo "    systemctl --user start fluxer-presence"
fi

if ! systemctl --user is-active --quiet graphical-session.target; then
    echo
    echo "Note: your session doesn't activate graphical-session.target, so the service"
    echo "won't start at login on its own. Add this to your compositor's autostart:"
    echo "    systemctl --user import-environment WAYLAND_DISPLAY DISPLAY HYPRLAND_INSTANCE_SIGNATURE SWAYSOCK XDG_SESSION_ID; systemctl --user restart fluxer-presence"
    echo "  Hyprland (hyprland.conf): exec-once = sh -c 'systemctl --user import-environment WAYLAND_DISPLAY DISPLAY HYPRLAND_INSTANCE_SIGNATURE SWAYSOCK XDG_SESSION_ID; systemctl --user restart fluxer-presence'"
    echo "  Sway (config):            exec sh -c 'systemctl --user import-environment WAYLAND_DISPLAY DISPLAY HYPRLAND_INSTANCE_SIGNATURE SWAYSOCK XDG_SESSION_ID; systemctl --user restart fluxer-presence'"
fi
