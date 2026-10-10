# fluxer-presence

Discord-style "rich presence" for [Fluxer](https://fluxer.app) on Windows and Linux.

Fork of [Scouted444/fluxer-presence](https://github.com/Scouted444/fluxer-presence) that adds Linux support (Wayland and X11) and automatic Steam game detection.

Fluxer has no native Rich Presence or plugin API yet, so this is a small standalone script. It watches what is running on your PC and sets your Fluxer **custom status** to something like:

```
🎮 Playing Bully · 1h 23m
💻 Coding in VS Code — main.rs · 42m
```

It uses the documented `PATCH https://api.fluxer.app/v1/users/@me/settings` endpoint. No injection, no client mods. Once Fluxer ships native rich presence this will be obsolete.

## Features

- Zero dependencies: Python 3.9+ standard library only
- **Linux: any Steam game you launch shows up automatically**, native or Proton, using its real name
- **Picks up the Rich Presence games already have built in** (see below)
- Otherwise falls back to the focused app, then the highest-priority running app from your list
- Elapsed timer from the real process start time
- Optional window title per app (`"title": true`)
- Restores your original custom status when nothing matches, you go idle, or you quit
- Status carries an expiry, so it clears itself even if the script is killed
- Rate-limit aware (honours `Retry-After`), never updates more than every 30 s

## Setup (Windows)

1. Install [Python 3.9+](https://www.python.org/downloads/) (tick "Add to PATH").
2. Download this repo (or `git clone`).
3. Run `python fluxer_presence.py` once. It creates `config.json` and exits asking for a token.
4. Put your Fluxer token in `config.json` under `"token"`, or set the `FLUXER_TOKEN` environment variable.
5. Edit `apps` in `config.json` to match the programs you use.
6. Run `python fluxer_presence.py` again. Leave it running.

Test safely first with `python fluxer_presence.py --dry-run`, which prints what it would set and never contacts Fluxer.

### Run at login, hidden

Press `Win+R`, type `shell:startup`, and drop a shortcut to `start_hidden.bat` in that folder. Stop it by ending `pythonw.exe` in Task Manager (your original status will expire on its own within 5 minutes).

## Setup (Linux)

```sh
git clone https://github.com/shadowshard4080/fluxer-presence
cd fluxer-presence
sh install.sh
```

`install.sh` creates `config.json` and installs a systemd user service that starts at login. Add your token to `config.json` (see below), then run `systemctl --user start fluxer-presence`.

- Logs: `journalctl --user -u fluxer-presence -f`
- After editing `config.json`: `systemctl --user restart fluxer-presence`
- Stop (restores your status): `systemctl --user stop fluxer-presence`
- Remove: `sh uninstall.sh`

Test first with `python3 fluxer_presence.py --dry-run`.

**Focused-window detection** uses `hyprctl` on Hyprland, `swaymsg` on Sway, or `xdotool` on X11. On other Wayland desktops (GNOME, KDE) the focused window can't be read, so apps are picked by `priority` instead; Steam games and game rich presence still work.

**Start at login:** the service starts with `graphical-session.target`, which GNOME, KDE and uwsm-managed sessions activate. If yours doesn't (e.g. plain Hyprland or Sway), `install.sh` prints a line to add to your compositor's autostart.

**Steam games:** detected from Steam's launcher, with the name read from the game's Steam files. Your own `apps` entry wins if the game is listed there. Non-game Steam apps can be skipped with `steam_ignore` (Steam app IDs; Lossless Scaling and Wallpaper Engine are ignored by default).

### Getting your token

The script edits your own account settings, so it needs your user token. You get it from the Fluxer website (not the desktop app):

1. Open [fluxer.app](https://fluxer.app) in your browser and log in.
2. Press **F12** or **Ctrl+Shift+I** to open DevTools and go to the **Network** tab.
3. Type `api.fluxer.app` in the filter box. If the list is empty, click around in Fluxer (open a channel or server) so requests appear.
4. Click any request in the list, then find **Request Headers** in the Headers panel.
5. Copy the value next to `Authorization` (only the value, not the word "Authorization").
6. Paste it into `config.json` between the quotes: `"token": "your-token-here"`.

> **Treat your token like your password.** Anyone who has it can control your Fluxer account. Never share it, post it in screenshots, or send it to anyone, including people claiming to help with this script. `config.json` is in `.gitignore` so it won't be committed by accident.

## Game rich presence (built into games)

Most games and many apps already send their Discord Rich Presence to a local pipe, `\\.\pipe\discord-ipc-0` to `-9` (on Linux, the socket `$XDG_RUNTIME_DIR/discord-ipc-0`). This script opens that pipe and speaks the same protocol (handshake, `SET_ACTIVITY`, ping), so the game thinks it is talking to Discord. A game's `details`, `state`, activity type and start time become your Fluxer status:

```
🎮 Playing Bully — Chapter 2 · In class · 1h 01m
```

The game's display name comes from your `apps` list if the exe is there, otherwise from the exe file name. When a game sends nothing, the script falls back to plain app detection.

**Discord has to be closed.** Games connect to the first pipe they can open, and Discord grabs `discord-ipc-0`. If Discord is running, the script logs a warning and games will keep talking to Discord. If you need both, use [fluxer-rpc](https://github.com/letruxux/fluxer-rpc) instead.

Set `"rpc_enabled": false` to turn this off.

On Linux, Windows games running through Proton/Wine can't reach this socket, which is why Steam games are also detected directly.

## Config reference

| Key | Default | Meaning |
|---|---|---|
| `token` | `""` | Your Fluxer token (or use `FLUXER_TOKEN`) |
| `poll_seconds` | `2` | How often to scan for apps (local only) |
| `min_update_seconds` | `30` | Minimum gap between API calls |
| `status_ttl_minutes` | `5` | Status auto-expires this long after the last push |
| `idle_minutes` | `10` | No input for this long clears the status (`0` = off) |
| `restore_original_status` | `true` | Put your previous custom status back |
| `rpc_enabled` | `true` | Listen for games' built-in Discord Rich Presence |
| `steam_games` | `true` | Linux: show running Steam games automatically |
| `steam_ignore` | `[993090, 431960]` | Linux: Steam app IDs to never show |
| `apps` | see `config.example.json` | `exe name → {name, verb, emoji, priority, title?}` |

Exe names are matched case-insensitively. On Linux, keys are program names (`firefox`, `code`, `blender`); a `.exe` key like `firefox.exe` also matches native `firefox`, and Proton games match their `.exe` name. Lower `priority` wins when nothing listed is focused. Emoji must be plain Unicode (custom emoji need an ID and an account entitlement).

## Limits and warnings

- **Keep `config.json` private.** Anyone with your token controls your account. It is in `.gitignore`; don't remove that.
- Automating a user account may not be allowed by Fluxer's rules. You use this at your own risk.
- Fluxer only has a custom status line, so you get text and one emoji, not Discord-style cards with images, buttons, or party info.
- Only the most recently updated game is shown if several are running.
- **Idle detection on Linux:** works on X11 with `xprintidle`; on Wayland only if your idle daemon sets logind's idle hint. Otherwise it's off.

## Related

- [FluxPresence](https://github.com/g00bPy/FluxPresence): GUI status automator
- [fluxer-rpc](https://github.com/letruxux/fluxer-rpc): mirrors real Discord Rich Presence to Fluxer

Not affiliated with Fluxer. MIT licensed.
