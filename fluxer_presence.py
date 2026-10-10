#!/usr/bin/env python3
r"""
fluxer_presence.py - Discord-style "rich presence" for Fluxer (Windows and Linux).

Fluxer has no native Rich Presence / plugin API yet, so this runs as a small
standalone background script. It watches what's running on your PC and sets your
Fluxer custom status to something like:

    🎮 Playing Bully · 1h 23m

Features
  - Zero dependencies (Python 3.9+ stdlib; ctypes on Windows, /proc on Linux)
  - Linux: running Steam games (native or Proton) are detected automatically
  - Picks up the Discord Rich Presence that games/apps already have built in: it
    listens on Discord's local IPC pipe (\\.\pipe\discord-ipc-N, or
    $XDG_RUNTIME_DIR/discord-ipc-N on Linux), so a game's
    "Playing X - details - state" shows up on Fluxer with no per-game setup
    (requires Discord to be closed, otherwise games talk to Discord instead)
  - Game rich presence wins; otherwise the focused app, then highest-priority running app
  - Elapsed timer based on the real process start time
  - Optional window title (e.g. the file open in VS Code)
  - Clears to your ORIGINAL custom status when nothing matches / you go idle / you exit
  - Status carries an expiry, so it self-clears even if the script gets killed
  - Respects rate limits (429 Retry-After)

Setup
  1. python fluxer_presence.py        (creates config.json next to the script)
  2. Put your Fluxer token in config.json ("token") or set env var FLUXER_TOKEN
  3. python fluxer_presence.py        (leave it running; Windows: pythonw.exe hides the console,
                                       Linux: sh install.sh sets up a systemd user service)

Test without touching your account:  python fluxer_presence.py --dry-run

Endpoint used: PATCH https://api.fluxer.app/v1/users/@me/settings  {"custom_status": {...}}
"""

import asyncio
import atexit
import ctypes
import itertools
import json
import os
import re
import signal
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_URL = "https://api.fluxer.app/v1/users/@me/settings"
CONFIG_PATH = Path(__file__).with_name("config.json")
MAX_STATUS_LEN = 128

DEFAULT_CONFIG = {
    "token": "",
    "poll_seconds": 2,            # how often to scan for apps (local only, no network)
    "min_update_seconds": 30,     # never hit the API more often than this
    "status_ttl_minutes": 5,      # status auto-expires this long after the last push
    "idle_minutes": 10,           # no keyboard/mouse for this long = clear status (0 = off)
    "restore_original_status": True,
    "rpc_enabled": True,          # listen for games' built-in Discord Rich Presence
    "steam_games": True,          # Linux: show any running Steam game automatically
    # Steam app IDs that aren't games (993090 = Lossless Scaling, 431960 = Wallpaper Engine)
    "steam_ignore": [993090, 431960],
    # Lower priority number wins when several listed apps run and none is focused.
    # verb + name + timer are joined as "<verb> <name> · <timer>".
    # "title": true appends the window title of that app's focused window.
    "apps": {
        "bully.exe":            {"name": "Bully",           "verb": "Playing",   "emoji": "🎮", "priority": 1},
        "gmod.exe":             {"name": "Garry's Mod",     "verb": "Playing",   "emoji": "🎮", "priority": 1},
        "robloxplayerbeta.exe": {"name": "Roblox",          "verb": "Playing",   "emoji": "🎮", "priority": 1},
        "javaw.exe":            {"name": "Minecraft",       "verb": "Playing",   "emoji": "⛏️", "priority": 1},
        "blender.exe":          {"name": "Blender",         "verb": "Modeling in", "emoji": "🧊", "priority": 2},
        "unity.exe":            {"name": "Unity",           "verb": "Working in", "emoji": "🛠️", "priority": 2},
        "godot.exe":            {"name": "Godot",           "verb": "Working in", "emoji": "🛠️", "priority": 2},
        "code.exe":             {"name": "VS Code",         "verb": "Coding in", "emoji": "💻", "priority": 3, "title": True},
        "chrome.exe":           {"name": "Chrome",          "verb": "Browsing with", "emoji": "🌐", "priority": 9},
        "firefox.exe":          {"name": "Firefox",         "verb": "Browsing with", "emoji": "🌐", "priority": 9},
    },
}


# --------------------------------------------------------------------------- #
# Windows helpers (all ctypes, no pywin32/psutil)
# --------------------------------------------------------------------------- #
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class Win:
    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self):
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        u = self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.INVALID = ctypes.c_void_p(-1).value

        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = ctypes.c_void_p
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetTickCount.restype = wintypes.DWORD
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
        u.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]

    def running(self, wanted):
        """Return {exe_lower: [pid, ...]} for processes whose exe name is in `wanted`."""
        found = {}
        snap = self.k.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == self.INVALID:
            return found
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = self.k.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile.lower()
                if name in wanted:
                    found.setdefault(name, []).append(entry.th32ProcessID)
                ok = self.k.Process32NextW(snap, ctypes.byref(entry))
        finally:
            self.k.CloseHandle(snap)
        return found

    def _open(self, pid):
        return self.k.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)

    def start_time(self, pid):
        """Unix timestamp the process started at, or None."""
        h = self._open(pid)
        if not h:
            return None
        try:
            c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
            if not self.k.GetProcessTimes(h, *(ctypes.byref(x) for x in (c, e, kt, ut))):
                return None
            ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
            return ticks / 1e7 - 11644473600
        finally:
            self.k.CloseHandle(h)

    def foreground(self):
        """(exe_lower, window_title) of the focused window, or (None, '')."""
        hwnd = self.u.GetForegroundWindow()
        if not hwnd:
            return None, ""
        pid = wintypes.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(512)
        self.u.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        return self.exe_of(pid.value), title

    def exe_of(self, pid):
        """Lower-case exe file name for a pid, or None."""
        h = self._open(pid)
        if not h:
            return None
        try:
            path = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not self.k.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                return None
            return os.path.basename(path.value).lower()
        finally:
            self.k.CloseHandle(h)

    def idle_seconds(self):
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not self.u.GetLastInputInfo(ctypes.byref(info)):
            return 0
        return ((self.k.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


# --------------------------------------------------------------------------- #
# Linux helpers (/proc, plus hyprctl/swaymsg/xdotool for the focused window)
#
# Exe names are matched against the binary name, argv[0] (Wine/Proton games show
# up as "Bully.exe" here) and comm. A config key "firefox.exe" also matches a
# native "firefox", so the same config works on both platforms.
# --------------------------------------------------------------------------- #
class Linux:
    def __init__(self):
        import shutil
        self.shutil = shutil
        self.hz = os.sysconf("SC_CLK_TCK")
        self.btime = 0.0
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    self.btime = float(line.split()[1])
        self.idle_warned = False

    @staticmethod
    def _names(pid):
        names = []
        try:
            names.append(os.path.basename(os.readlink(f"/proc/{pid}/exe")))
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "replace")
            if argv0:
                names.append(re.split(r"[\\/]", argv0)[-1])
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/comm") as f:
                names.append(f.read().strip())
        except OSError:
            pass
        return [n.lower() for n in names if n]

    @staticmethod
    def _match(names, wanted):
        for n in names:
            if n in wanted:
                return n
            if n + ".exe" in wanted:
                return n + ".exe"
        return None

    def running(self, wanted):
        """Return {config_key: [pid, ...]} for processes matching a key in `wanted`."""
        found = {}
        me = os.getpid()
        for d in os.listdir("/proc"):
            if not d.isdigit() or int(d) == me:
                continue
            key = self._match(self._names(d), wanted)
            if key:
                found.setdefault(key, []).append(int(d))
        return found

    def start_time(self, pid):
        """Unix timestamp the process started at, or None."""
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            fields = stat[stat.rindex(")") + 2:].split()
            return self.btime + int(fields[19]) / self.hz
        except (OSError, ValueError, IndexError):
            return None

    def _run(self, *cmd):
        import subprocess
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def foreground(self):
        """(name_lower, window_title) of the focused window, or (None, '')."""
        pid, title = None, ""
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and self.shutil.which("hyprctl"):
            try:
                w = json.loads(self._run("hyprctl", "activewindow", "-j") or "{}")
                pid, title = w.get("pid"), w.get("title", "")
            except ValueError:
                pass
        elif os.environ.get("SWAYSOCK") and self.shutil.which("swaymsg"):
            try:
                def walk(n):
                    if n.get("focused") and n.get("pid"):
                        return n
                    for c in n.get("nodes", []) + n.get("floating_nodes", []):
                        r = walk(c)
                        if r:
                            return r
                w = walk(json.loads(self._run("swaymsg", "-t", "get_tree") or "{}")) or {}
                pid, title = w.get("pid"), w.get("name") or ""
            except ValueError:
                pass
        elif os.environ.get("DISPLAY") and self.shutil.which("xdotool"):
            out = self._run("xdotool", "getactivewindow", "getwindowpid", "getwindowname").splitlines()
            if len(out) >= 2 and out[0].strip().isdigit():
                pid, title = int(out[0]), out[1]
        if not isinstance(pid, int) or pid <= 0:
            return None, ""
        return self.exe_of(pid), title

    def exe_of(self, pid, wanted=None):
        """Config key (if `wanted` given and matched) or binary name for a pid, or None."""
        names = self._names(pid)
        if not names:
            return None
        if wanted is None:
            wanted = getattr(self, "wanted", None) or ()
        return self._match(names, wanted) or names[-1]

    def steam_games(self):
        """{appid: [pid]} for running Steam games (each is launched under Steam's `reaper`)."""
        found = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/comm") as f:
                    if f.read().strip() != "reaper":
                        continue
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    argv = f.read().decode("utf-8", "replace").split("\0")
            except OSError:
                continue
            if "SteamLaunch" in argv:
                appid = next((a[6:] for a in argv if a.startswith("AppId=")), None)
                if appid and appid.isdigit() and appid != "0":
                    found.setdefault(appid, []).append(int(d))
        return found

    def steam_name(self, appid):
        """Game name from its appmanifest, or None. Cached."""
        cache = self.__dict__.setdefault("_steam_names", {})
        if appid in cache:
            return cache[appid]
        name = None
        roots = [os.path.expanduser(p) for p in (
            "~/.local/share/Steam", "~/.steam/steam",
            "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")]
        libs = list(roots)
        for root in roots:
            try:
                with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as f:
                    libs += re.findall(r'"path"\s+"([^"]+)"', f.read())
            except OSError:
                pass
        for lib in dict.fromkeys(libs):
            try:
                with open(os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf"), encoding="utf-8") as f:
                    m = re.search(r'"name"\s+"([^"]+)"', f.read())
            except OSError:
                continue
            if m:
                name = m.group(1).replace("™", "").replace("®", "").strip()
                break
        cache[appid] = name
        return name

    def idle_seconds(self):
        # X11: xprintidle. Wayland: logind's IdleHint (set by e.g. hypridle/swayidle
        # running `loginctl lock-session` or similar). Otherwise idle detection is off.
        if os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY") \
                and self.shutil.which("xprintidle"):
            out = self._run("xprintidle").strip()
            if out.isdigit():
                return int(out) / 1000.0
        sid = os.environ.get("XDG_SESSION_ID")
        if sid and self.shutil.which("loginctl"):
            props = dict(l.split("=", 1) for l in self._run(
                "loginctl", "show-session", sid, "-p", "IdleHint",
                "-p", "IdleSinceHintMonotonic").splitlines() if "=" in l)
            if props.get("IdleHint") == "yes":
                since = int(props.get("IdleSinceHintMonotonic") or 0) / 1e6
                return max(0.0, time.monotonic() - since) if since else float("inf")
            return 0
        return 0


# --------------------------------------------------------------------------- #
# Fluxer API
# --------------------------------------------------------------------------- #
class Fluxer:
    def __init__(self, token, dry_run=False):
        self.token = token
        self.dry_run = dry_run
        self.blocked_until = 0.0  # rate-limit cool-down

    def _request(self, method, body=None):
        req = urllib.request.Request(
            API_URL,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
                "User-Agent": "fluxer-presence/1.0 (+standalone status script)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5.0
                try:
                    wait = float(e.headers.get("Retry-After") or json.loads(e.read()).get("retry_after", 5))
                except Exception:
                    pass
                self.blocked_until = time.time() + wait + 1
                log(f"rate limited, backing off {wait:.0f}s")
                return None
            if e.code in (401, 403):
                log(f"auth rejected by Fluxer (HTTP {e.code}). Check your token.")
                if e.code == 401:
                    sys.exit(1)
            else:
                log(f"HTTP {e.code} from Fluxer")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"network error: {e}")
            return None

    def get_custom_status(self):
        if self.dry_run:
            return None
        data = self._request("GET")
        return (data or {}).get("custom_status")

    def set_custom_status(self, status):
        """status = dict(text, emoji_name?, expires_at?) or None to clear."""
        if self.dry_run:
            log(f"[dry-run] would set custom_status = {status}")
            return True
        if time.time() < self.blocked_until:
            return False
        return self._request("PATCH", {"custom_status": status}) is not None


# --------------------------------------------------------------------------- #
# Discord-compatible local RPC server (what games' built-in Rich Presence talks to)
#
# Wire format on the pipe: [uint32 LE opcode][uint32 LE length][JSON payload]
#   opcodes: 0 handshake, 1 frame, 2 close, 3 ping, 4 pong
# --------------------------------------------------------------------------- #
OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = range(5)
MAX_FRAME = 1024 * 1024
RPC_VERBS = {0: "Playing", 1: "Streaming", 2: "Listening to", 3: "Watching", 5: "Competing in"}
RPC_EMOJI = {0: "🎮", 1: "📡", 2: "🎧", 3: "📺", 5: "🏆"}


def encode_frame(op, payload):
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("<II", op, len(data)) + data


class RpcState:
    """Thread-safe store of the activities currently set by connected games."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}  # connection id -> entry dict

    def set(self, cid, entry):
        with self.lock:
            entry["updated"] = time.time()
            self.entries[cid] = entry

    def clear(self, cid):
        with self.lock:
            self.entries.pop(cid, None)

    def current(self):
        with self.lock:
            if not self.entries:
                return None
            return max(self.entries.values(), key=lambda e: e["updated"])


class RpcProtocol(asyncio.Protocol):
    _ids = itertools.count(1)

    def __init__(self, state):
        self.state = state
        self.cid = next(self._ids)
        self.transport = None
        self.buf = b""
        self.client_id = None
        self.connected_at = time.time()

    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        self.state.clear(self.cid)

    def data_received(self, data):
        self.buf += data
        while len(self.buf) >= 8:
            op, length = struct.unpack("<II", self.buf[:8])
            if length > MAX_FRAME:
                return self._close()
            if len(self.buf) < 8 + length:
                return
            body, self.buf = self.buf[8:8 + length], self.buf[8 + length:]
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, ValueError):
                return self._close()
            self._handle(op, payload, body)

    def _send(self, op, payload):
        if self.transport and not self.transport.is_closing():
            self.transport.write(encode_frame(op, payload))

    def _close(self):
        if self.transport and not self.transport.is_closing():
            self.transport.close()

    def _handle(self, op, payload, raw):
        if op == OP_HANDSHAKE:
            self.client_id = str(payload.get("client_id", ""))
            self._send(OP_FRAME, {
                "cmd": "DISPATCH", "evt": "READY", "nonce": None,
                "data": {
                    "v": 1,
                    "config": {"cdn_host": "cdn.discordapp.com", "api_endpoint": "//discord.com/api",
                               "environment": "production"},
                    "user": {"id": "1", "username": "fluxer", "discriminator": "0", "avatar": None},
                },
            })
        elif op == OP_PING:
            if self.transport and not self.transport.is_closing():
                self.transport.write(struct.pack("<II", OP_PONG, len(raw)) + raw)
        elif op == OP_CLOSE:
            self._close()
        elif op == OP_FRAME:
            cmd, nonce = payload.get("cmd"), payload.get("nonce")
            if cmd == "SET_ACTIVITY":
                args = payload.get("args") or {}
                act = args.get("activity")
                if act:
                    self.state.set(self.cid, {"activity": act, "pid": args.get("pid"),
                                              "client_id": self.client_id,
                                              "connected_at": self.connected_at})
                else:
                    self.state.clear(self.cid)
                self._send(OP_FRAME, {"cmd": cmd, "data": act, "evt": None, "nonce": nonce})
            else:  # SUBSCRIBE, GET_USER, etc.: ack with an empty result
                self._send(OP_FRAME, {"cmd": cmd, "data": {}, "evt": None, "nonce": nonce})


def unix_ipc_path(i):
    # Same lookup order discord-rpc clients use
    base = next((os.environ[v] for v in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP") if os.environ.get(v)),
                "/tmp")
    return os.path.join(base, f"discord-ipc-{i}")


def claim_unix_socket(path):
    """True if `path` is free (removing a stale socket left by a dead process)."""
    import socket
    if not os.path.exists(path):
        return True
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return False  # something (Discord) is listening
    except OSError:
        os.unlink(path)
        return True
    finally:
        s.close()


def start_rpc_server(state):
    """Open the first free discord-ipc-N pipe. Returns N, or None if all are taken."""
    result = {}
    ready = threading.Event()

    def run():
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for i in range(10):
            try:
                if sys.platform == "win32":
                    loop.run_until_complete(
                        loop.start_serving_pipe(lambda: RpcProtocol(state), rf"\\.\pipe\discord-ipc-{i}"))
                else:
                    path = unix_ipc_path(i)
                    if not claim_unix_socket(path):
                        continue
                    loop.run_until_complete(loop.create_unix_server(lambda: RpcProtocol(state), path))
                    atexit.register(lambda p=path: os.path.exists(p) and os.unlink(p))
                result["index"] = i
                break
            except OSError:  # PermissionError if another process (Discord) owns this pipe
                continue
        ready.set()
        if "index" in result:
            loop.run_forever()

    threading.Thread(target=run, daemon=True, name="rpc-server").start()
    ready.wait(5)
    return result.get("index")


# --------------------------------------------------------------------------- #
# Logic
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"created {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["apps"] = {k.lower(): v for k, v in merged["apps"].items()}
    return merged


def fmt_elapsed(seconds):
    m = int(max(0, seconds) // 60)
    if m < 1:
        return "just started"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def clean_title(title, app_name):
    # "main.rs - myproject - Visual Studio Code" -> "main.rs" (first segment only)
    title = re.sub(r"\s+", " ", title).strip()
    return re.split(r"\s[-–—]\s", title, maxsplit=1)[0].strip()


def pick_app(win, cfg):
    """Return (exe, app_cfg, start_ts, title) for the app to show, or None."""
    apps = cfg["apps"]
    fg_exe, fg_title = win.foreground()
    running = win.running(set(apps))

    # Steam games are picked up automatically, unless one is already in `apps`
    if cfg.get("steam_games", True) and hasattr(win, "steam_games"):
        configured_game = any(apps[e].get("verb") == "Playing" for e in running)
        if not configured_game:
            apps = dict(apps)
            ignore = {str(a) for a in cfg.get("steam_ignore", [])}
            for appid, pids in win.steam_games().items():
                name = None if appid in ignore else win.steam_name(appid)
                if name:
                    key = f"steam:{appid}"
                    apps[key] = {"name": name, "verb": "Playing", "emoji": "🎮", "priority": 1}
                    running[key] = pids
    if not running:
        return None

    if fg_exe in running:
        exe = fg_exe
    else:
        exe = min(running, key=lambda e: apps[e].get("priority", 5))

    start = min((t for t in (win.start_time(p) for p in running[exe]) if t), default=time.time())
    title = fg_title if (exe == fg_exe and apps[exe].get("title")) else ""
    return exe, apps[exe], start, title


def build_status(exe, app, start, title, cfg):
    name = app.get("name", exe)
    text = f'{app.get("verb", "Using")} {name} · {fmt_elapsed(time.time() - start)}'
    t = clean_title(title, name) if title else ""
    if t:
        text = f"{app.get('verb', 'Using')} {name} — {t} · {fmt_elapsed(time.time() - start)}"
    return finalize(text, app.get("emoji"), cfg)


def finalize(text, emoji, cfg):
    if len(text) > MAX_STATUS_LEN:
        text = text[: MAX_STATUS_LEN - 1] + "…"
    status = {"text": text}
    if emoji:
        status["emoji_name"] = emoji
    ttl = float(cfg["status_ttl_minutes"])
    if ttl > 0:
        exp = datetime.now(timezone.utc) + timedelta(minutes=ttl)
        status["expires_at"] = exp.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return status


def build_rpc_status(entry, win, cfg):
    """Turn a game's SET_ACTIVITY payload into a Fluxer custom status."""
    act = entry["activity"]
    atype = act.get("type") if isinstance(act.get("type"), int) else 0
    pid = entry.get("pid")
    exe = win.exe_of(pid) if isinstance(pid, int) else None

    app = cfg["apps"].get(exe or "", {})
    name = app.get("name") or (os.path.splitext(exe)[0].replace("_", " ").title() if exe else "a game")

    details, state = act.get("details"), act.get("state")
    detail = " · ".join(str(x).strip() for x in (details, state) if x and str(x).strip())

    start = (act.get("timestamps") or {}).get("start")
    if isinstance(start, (int, float)) and start > 0:
        start = start / 1000 if start > 1e12 else start  # some games send milliseconds
    else:
        start = (win.start_time(pid) if isinstance(pid, int) else None) or entry["connected_at"]

    verb = RPC_VERBS.get(atype, "Playing")
    text = f"{verb} {name}"
    if detail:
        text += f" — {detail}"
    text += f" · {fmt_elapsed(time.time() - start)}"
    return finalize(text, app.get("emoji") or RPC_EMOJI.get(atype, "🎮"), cfg)


def usable_original(status):
    """Return the user's original custom status if it's still valid, else None."""
    if not status:
        return None
    exp = status.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(exp.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                return None
        except ValueError:
            pass
    keep = {k: status[k] for k in ("text", "emoji_id", "emoji_name", "expires_at") if status.get(k)}
    return keep or None


def main():
    if sys.platform not in ("win32", "linux"):
        print("This script supports Windows and Linux only.")
        return 1
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    token = (os.environ.get("FLUXER_TOKEN") or cfg.get("token") or "").strip()
    if not token and not dry_run:
        print(f"No token set. Put it in {CONFIG_PATH} (\"token\") or set FLUXER_TOKEN, then rerun.")
        return 1

    win = Win() if sys.platform == "win32" else Linux()
    win.wanted = set(cfg["apps"])
    fx = Fluxer(token, dry_run)
    original = usable_original(fx.get_custom_status()) if cfg["restore_original_status"] else None
    state = {"showing": False}

    def cleanup(*_):
        if state["showing"]:
            fx.blocked_until = 0
            fx.set_custom_status(original)
            state["showing"] = False
            log("status restored, bye")

    atexit.register(cleanup)
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: sys.exit(0))

    # restore status when the console window is closed with the X button
    if sys.platform == "win32":
        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        handler = HANDLER(lambda ev: (cleanup(), False)[1])
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    elif hasattr(signal, "SIGHUP"):  # terminal closed
        signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))

    rpc_state = RpcState()
    if cfg.get("rpc_enabled", True):
        idx = start_rpc_server(rpc_state)
        if idx is None:
            log("could not open any discord-ipc pipe; game rich presence disabled")
        elif idx == 0:
            log("listening for games' built-in rich presence on discord-ipc-0")
        else:
            log(f"Discord is running (pipe 0 is taken), so games will connect to Discord first. "
                f"Close Discord for games to reach this script (listening on discord-ipc-{idx}).")

    log("running - Ctrl+C to quit" + (" (dry run)" if dry_run else ""))
    last_key, last_push = None, 0.0
    poll = max(0.5, float(cfg["poll_seconds"]))
    min_gap = max(5.0, float(cfg["min_update_seconds"]))
    heartbeat = max(60.0, float(cfg["status_ttl_minutes"]) * 30)  # refresh before expiry

    while True:
        try:
            idle_limit = float(cfg["idle_minutes"]) * 60
            idle = idle_limit > 0 and win.idle_seconds() >= idle_limit
            status = None
            if not idle:
                entry = rpc_state.current()
                if entry:
                    status = build_rpc_status(entry, win, cfg)
                else:
                    pick = pick_app(win, cfg)
                    if pick:
                        status = build_status(*pick, cfg)
            now = time.time()

            if status is None:
                if state["showing"] and now - last_push >= 5 and fx.set_custom_status(original):
                    state.update(showing=False)
                    last_key, last_push = None, now
                    log("no activity - status restored")
            else:
                key = status["text"]
                due = key != last_key and now - last_push >= min_gap
                stale = now - last_push >= heartbeat
                if (due or stale) and fx.set_custom_status(status):
                    state.update(showing=True)
                    last_key, last_push = key, now
                    log(f"status -> {key}")
        except SystemExit:
            raise
        except Exception as e:  # never let one bad scan kill the loop
            log(f"error: {e!r}")
        time.sleep(poll)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
