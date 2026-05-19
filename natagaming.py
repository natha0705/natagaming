#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  natagaming.py — Tier S Gaming Daemon                       ║
║  Modos: auto | steam | run | wine | proton | tty            ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ── Runtime dir ────────────────────────────────────────────────
XDG_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
RUNTIME_DIR = XDG_RUNTIME_DIR / "natagaming"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

HYPR_STATE_FILE = RUNTIME_DIR / "hyprstate"
CPU_STATE_FILE  = RUNTIME_DIR / "cpustate"
WAYBAR_PID_FILE = RUNTIME_DIR / "waybar.pid"
LOCK_FILE       = RUNTIME_DIR / "natagaming.lock"
PID_FILE        = RUNTIME_DIR / "natagaming.pid"
MPV_SOCKET      = RUNTIME_DIR / "mpv.sock"
MPV_PID_FILE    = RUNTIME_DIR / "mpv.pid"

DAEMON_STATE_FILE = RUNTIME_DIR / "daemon_state.json"

XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_FILE     = XDG_CONFIG_HOME / "natagaming.conf"

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    format="[natagaming] %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("natagaming")

def setup_logging(level: str) -> None:
    levels = {"debug": logging.DEBUG, "info": logging.INFO,
              "warn": logging.WARNING, "error": logging.ERROR}
    logger.setLevel(levels.get(level, logging.INFO))

_last_notify_ts: float = 0.0
_NOTIFY_RATE_LIMIT_S   = 2.0

def notify(title: str, body: str = "", urgency: str = "low", timeout_ms: int = 2000) -> None:
    global _last_notify_ts
    if urgency != "critical":
        now = time.monotonic()
        if now - _last_notify_ts < _NOTIFY_RATE_LIMIT_S:
            logger.debug("notify: rate-limited ('%s')", title)
            return
        _last_notify_ts = now
    try:
        subprocess.Popen(
            ["notify-send", title, body, "-u", urgency,
             "-t", str(timeout_ms), "-i", "applications-games", "-a", "natagaming"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

# ── Config ─────────────────────────────────────────────────────
VALID_SCALAR_KEYS = {
    "PLAYLIST", "GAMING_TTY", "GAMESCOPE_RES", "GAMESCOPE_HZ",
    "PROTON_PATH", "STEAM_COMPAT_DATA_PATH", "STEAM_COMPAT_CLIENT_INSTALL_PATH",
    "ENABLE_MPV", "ENABLE_WAYBAR", "ENABLE_SPOTIFY_PAUSE", "ENABLE_CPU_GOVERNOR",
    "LOG_LEVEL", "DEBOUNCE_MS", "NOTIFY_TIMEOUT", "CONFIG_VERSION",
    "SPOTIFY_PLAYER_NAME",
}
VALID_ARRAY_KEYS = {
    "GAMING_WINDOW_CLASSES", "IGNORE_WINDOW_CLASSES", "GAMESCOPE_FLAGS", "TTY_APPS",
}
INT_KEYS = {
    "GAMING_TTY", "GAMESCOPE_HZ", "DEBOUNCE_MS", "NOTIFY_TIMEOUT",
    "CONFIG_VERSION", "ENABLE_MPV", "ENABLE_WAYBAR",
    "ENABLE_SPOTIFY_PAUSE", "ENABLE_CPU_GOVERNOR",
}

_ALLOWED_HYPR_OPTIONS: set[str] = {
    "decoration:blur:enabled",
    "decoration:drop_shadow",
    "animations:enabled",
}
_HYPR_VALUE_MAX = 1

def _validate_hypr_batch_keyword(kw: str) -> bool:
    parts = kw.split(" ", 1)
    if len(parts) != 2:
        return False
    option, value = parts
    if option not in _ALLOWED_HYPR_OPTIONS:
        return False
    if not re.fullmatch(r"\d+", value):
        return False
    if int(value) > _HYPR_VALUE_MAX:
        logger.warning("hypr batch: valor fuera de rango para '%s': %s", option, value)
        return False
    return True

class Config:
    def __init__(self) -> None:
        self.PLAYLIST                        = "https://www.youtube.com/watch?v=dDd-EkT5tuw&list=RDdDd-EkT5tuw&start_radio=1"
        self.GAMING_TTY: int                 = 2
        self.GAMESCOPE_RES: str              = "1920x1080"
        self.GAMESCOPE_HZ: int               = 144
        self.GAMESCOPE_FLAGS: list[str]      = []
        self.GAMING_WINDOW_CLASSES: list[str]= []
        self.IGNORE_WINDOW_CLASSES: list[str]= []
        self.TTY_APPS: list[str]             = []
        self.PROTON_PATH: str                = ""
        self.STEAM_COMPAT_DATA_PATH: str     = ""
        self.STEAM_COMPAT_CLIENT_INSTALL_PATH: str = str(Path.home() / ".steam/root")
        self.ENABLE_MPV: int                 = 1
        self.ENABLE_WAYBAR: int              = 1
        self.ENABLE_SPOTIFY_PAUSE: int       = 1
        self.ENABLE_CPU_GOVERNOR: int        = 1
        self.LOG_LEVEL: str                  = "info"
        self.DEBOUNCE_MS: int                = 150
        self.NOTIFY_TIMEOUT: int             = 2000
        self.CONFIG_VERSION: int             = 2
        self.SPOTIFY_PLAYER_NAME: str        = ""
        self._compiled_gaming: list[re.Pattern] = []
        self._compiled_ignore: list[re.Pattern] = []

    def load(self, path: Path) -> None:
        if not path.exists():
            self._write_default(path)
            return
        self._parse(path)
        self._validate()

    def _parse(self, path: Path) -> None:
        with path.open() as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")

                if key in VALID_ARRAY_KEYS:
                    PLACEHOLDER = "\x00PIPE\x00"
                    escaped = val.replace("\\|", PLACEHOLDER)
                    items = [
                        v.strip().strip('"').strip("'").replace(PLACEHOLDER, "|")
                        for v in escaped.split("|") if v.strip()
                    ]
                    setattr(self, key, items)
                    continue

                if key not in VALID_SCALAR_KEYS:
                    logger.warning("Config: clave desconocida '%s', ignorando", key)
                    continue

                if key in INT_KEYS:
                    if not re.fullmatch(r"\d+", val):
                        logger.warning("Config: '%s' debe ser numérico, ignorando", key)
                        continue
                    setattr(self, key, int(val))
                elif key == "GAMESCOPE_RES":
                    if not re.fullmatch(r"\d+x\d+", val):
                        logger.warning("Config: GAMESCOPE_RES formato inválido, ignorando")
                        continue
                    self.GAMESCOPE_RES = val
                elif key == "LOG_LEVEL":
                    if val not in ("debug", "info", "warn", "error"):
                        logger.warning("Config: LOG_LEVEL inválido, ignorando")
                        continue
                    self.LOG_LEVEL = val
                else:
                    setattr(self, key, val)

    def _validate(self) -> None:
        ok = True
        if not re.fullmatch(r"\d+", str(self.GAMING_TTY)):
            logger.error("Config: GAMING_TTY inválido")
            ok = False
        if not re.fullmatch(r"\d+x\d+", self.GAMESCOPE_RES):
            logger.error("Config: GAMESCOPE_RES inválido")
            ok = False
        if not self.STEAM_COMPAT_DATA_PATH:
            logger.warning("STEAM_COMPAT_DATA_PATH vacío — Proton puede fallar")
        self._compiled_gaming = self._compile_patterns("GAMING_WINDOW_CLASSES", self.GAMING_WINDOW_CLASSES)
        self._compiled_ignore = self._compile_patterns("IGNORE_WINDOW_CLASSES",  self.IGNORE_WINDOW_CLASSES)
        if not ok:
            sys.exit(1)

    @staticmethod
    def _compile_patterns(key: str, patterns: list[str]) -> list[re.Pattern]:
        compiled = []
        for pat in patterns:
            try:
                r = re.compile(pat)
                r.search("a" * 100)
                compiled.append(r)
            except re.error as e:
                logger.warning("Config: regex inválida en %s '%s': %s — ignorando", key, pat, e)
        return compiled

    def _write_default(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("""\
# natagaming.conf
# Arrays: pipe-separated → GAMING_WINDOW_CLASSES=steam_app_[0-9]+|cs2
# IMPORTANTE: el parser NO expande $HOME. Usa rutas absolutas.

LOG_LEVEL=info
DEBOUNCE_MS=150
NOTIFY_TIMEOUT=2000

GAMING_TTY=2
GAMESCOPE_RES=1920x1080
GAMESCOPE_HZ=144
GAMESCOPE_FLAGS=

ENABLE_MPV=1
PLAYLIST=https://www.youtube.com/watch?v=dDd-EkT5tuw&list=RDdDd-EkT5tuw&start_radio=1

ENABLE_WAYBAR=1
ENABLE_SPOTIFY_PAUSE=1
ENABLE_CPU_GOVERNOR=1

# Nombre del player de Spotify para playerctl.
# Dejar vacío para autodetectar (busca cualquier proceso que contenga "spotify").
SPOTIFY_PLAYER_NAME=

PROTON_PATH=
STEAM_COMPAT_DATA_PATH=
STEAM_COMPAT_CLIENT_INSTALL_PATH=

GAMING_WINDOW_CLASSES=steam_app_[0-9]+|cs2|hl2_linux|Minecraft|heroic|lutris|wine
IGNORE_WINDOW_CLASSES=firefox|Brave-browser|google-chrome|chromium|mpv|vlc|obs|discord|Spotify

TTY_APPS=
CONFIG_VERSION=2
""")
        logger.info("Config por defecto creada en %s", path)

# ── State ──────────────────────────────────────────────────────
class State:
    def __init__(self) -> None:
        self.gaming_active       = False
        self.cleaned             = False
        self.spotify_was_playing = False
        self.mpv_was_playing     = False
        self.debounce_task: Optional[asyncio.Task] = None
        self.shutdown_event: Optional[asyncio.Event] = None

        self._last_transition_ts: float = 0.0
        self._TRANSITION_COOLDOWN_S: float = 1.5
        self._last_event_ts: float = time.monotonic()

    def can_transition(self) -> bool:
        now = time.monotonic()
        if now - self._last_transition_ts < self._TRANSITION_COOLDOWN_S:
            logger.debug("State: transición bloqueada por cooldown")
            return False
        return True

    def mark_transition(self) -> None:
        self._last_transition_ts = time.monotonic()

# ── Persistent state ───────────────────────────────────────────
def save_daemon_state(state: State) -> None:
    data = {
        "gaming_active":       state.gaming_active,
        "spotify_was_playing": state.spotify_was_playing,
        "mpv_was_playing":     state.mpv_was_playing,
        "ts":                  time.time(),
        "pid":                 os.getpid(),
        "hyprland_sig":        os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""),
    }
    try:
        tmp = DAEMON_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(DAEMON_STATE_FILE)
    except Exception as e:
        logger.warning("save_daemon_state: %s", e)

def load_daemon_state() -> Optional[dict]:
    if not DAEMON_STATE_FILE.exists():
        return None
    try:
        data = json.loads(DAEMON_STATE_FILE.read_text())
        current_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        if data.get("hyprland_sig") and data["hyprland_sig"] != current_sig:
            logger.info("daemon_state: sesión diferente, descartando")
            DAEMON_STATE_FILE.unlink(missing_ok=True)
            return None
        return data
    except Exception as e:
        logger.warning("load_daemon_state: %s", e)
        return None

def clear_daemon_state() -> None:
    DAEMON_STATE_FILE.unlink(missing_ok=True)

# ── Instance lock ──────────────────────────────────────────────
_lock_fd: Optional[int] = None

def acquire_lock() -> None:
    global _lock_fd
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                PID_FILE.unlink(missing_ok=True)
                logger.info("PID file stale (PID %d) limpiado", old_pid)
        except (ValueError, OSError):
            PID_FILE.unlink(missing_ok=True)
    _lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[natagaming] Ya hay una instancia corriendo.", file=sys.stderr)
        sys.exit(1)
    os.ftruncate(_lock_fd, 0)
    os.lseek(_lock_fd, 0, 0)
    os.write(_lock_fd, str(os.getpid()).encode())
    PID_FILE.write_text(str(os.getpid()))

def release_lock() -> None:
    if _lock_fd is not None:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        os.close(_lock_fd)
    PID_FILE.unlink(missing_ok=True)

# ── Dependencies ───────────────────────────────────────────────
def check_deps(cfg: Config) -> None:
    required = ["hyprctl", "notify-send"]
    if cfg.ENABLE_MPV:           required.append("mpv")
    if cfg.ENABLE_SPOTIFY_PAUSE: required.append("playerctl")
    if cfg.ENABLE_CPU_GOVERNOR:
        if not shutil.which("powerprofilesctl") and not shutil.which("cpupower"):
            logger.warning("ENABLE_CPU_GOVERNOR=1 pero no se encontró powerprofilesctl ni cpupower")
    missing = [c for c in required if not shutil.which(c)]
    if missing:
        for c in missing:
            print(f"[natagaming] Falta dependencia: {c}", file=sys.stderr)
            print(f"[natagaming] Instala con: sudo pacman -S {c}", file=sys.stderr)
        sys.exit(1)

# ── Hyprland helpers ───────────────────────────────────────────
def get_socket_path() -> Path:
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not sig:
        print("[natagaming] Error: no estás dentro de una sesión Hyprland", file=sys.stderr)
        sys.exit(1)
    return XDG_RUNTIME_DIR / "hypr" / sig / ".socket2.sock"

def hyprctl(*args, timeout: int = 2) -> Optional[str]:
    try:
        r = subprocess.run(
            ["hyprctl", *args],
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout if r.returncode == 0 else None
    except subprocess.TimeoutExpired:
        logger.warning("hyprctl timeout")
        return None
    except Exception as e:
        logger.warning("hyprctl error: %s", e)
        return None

def get_hypr_value(option: str) -> Optional[int]:
    out = hyprctl("-j", "getoption", option)
    if not out:
        return None
    try:
        data = json.loads(out)
        if "int" in data:
            return data["int"]
        if "bool" in data:
            return data["bool"]
    except json.JSONDecodeError:
        pass
    return None

def hyprctl_batch(keywords: list[str]) -> bool:
    safe = []
    for k in keywords:
        if not _validate_hypr_batch_keyword(k):
            logger.warning("hyprctl_batch: keyword rechazada → '%s'", k)
            continue
        safe.append(k)
    if not safe:
        return False
    cmd = " ; ".join(f"keyword {k}" for k in safe)
    r = subprocess.run(
        ["hyprctl", "--batch", cmd],
        capture_output=True, text=True, timeout=2
    )
    if r.returncode != 0:
        logger.warning("hyprctl_batch: returncode=%d stderr=%s",
                       r.returncode, r.stderr.strip()[:200])
        return False
    had_error = False
    for line in r.stdout.splitlines():
        if line.strip().lower().startswith("error"):
            logger.warning("hyprctl_batch: error parcial → %s", line.strip())
            had_error = True
    return not had_error

def _snapshot_hypr_state() -> dict:
    return {
        "blur":   get_hypr_value("decoration:blur:enabled"),
        "shadow": get_hypr_value("decoration:drop_shadow"),
        "anim":   get_hypr_value("animations:enabled"),
    }

# ── FIX 1: save_hypr_state — no sobreescribir con valores gaming ──
def save_hypr_state() -> None:
    snapshot = _snapshot_hypr_state()
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")

    all_disabled = all(v == 0 for v in snapshot.values() if v is not None)

    # Si el archivo ya existe de esta sesión, no sobreescribir con gaming values (todos 0)
    if all_disabled and HYPR_STATE_FILE.exists():
        existing = load_hypr_state()
        if existing.get("sig") == sig:
            logger.warning(
                "save_hypr_state: snapshot con valores gaming detectados (todos 0), "
                "reutilizando estado previo para no sobreescribir valores reales"
            )
            return

    # Si todos son 0 pero NO hay archivo previo, puede ser que el usuario
    # tenga blur/shadows desactivados en su config normal — es correcto guardar 0.
    if all_disabled:
        logger.info(
            "save_hypr_state: blur/shadow/anim ya estaban en 0 — "
            "guardando como estado base (pueden estar desactivados en hyprland.conf)"
        )

    blur   = max(0, min(1, int(snapshot["blur"]   if snapshot["blur"]   is not None else 1)))
    shadow = max(0, min(1, int(snapshot["shadow"] if snapshot["shadow"] is not None else 1)))
    anim   = max(0, min(1, int(snapshot["anim"]   if snapshot["anim"]   is not None else 1)))

    content = (
        f"pid={os.getpid()}\n"
        f"sig={sig}\n"
        f"blur={blur}\n"
        f"shadow={shadow}\n"
        f"anim={anim}\n"
    )
    tmp = HYPR_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.replace(HYPR_STATE_FILE)

def load_hypr_state() -> dict:
    state = {"pid": None, "sig": "", "blur": 1, "shadow": 1, "anim": 1}
    if not HYPR_STATE_FILE.exists():
        return state
    for line in HYPR_STATE_FILE.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if k == "pid":
                try:    state["pid"] = int(v)
                except ValueError: pass
            elif k == "sig":
                state["sig"] = v
            else:
                try:
                    state[k] = max(0, min(1, int(v)))
                except ValueError: pass
    return state

# ── FIX 2: restore_hypr_state — retry + notificación crítica al usuario ──
def restore_hypr_state() -> bool:
    if not HYPR_STATE_FILE.exists():
        return False
    s = load_hypr_state()

    current_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    saved_sig   = s.get("sig", "")
    if saved_sig and saved_sig != current_sig:
        logger.warning(
            "hypr state pertenece a sesión '%s' (actual '%s') — descartando estado huérfano",
            saved_sig, current_sig
        )
        HYPR_STATE_FILE.unlink(missing_ok=True)
        return False

    saved_pid = s.get("pid")
    if saved_pid is not None and saved_pid != os.getpid():
        logger.warning(
            "hypr state pertenece a PID %d (actual %d) — restaurando (recovery post-crash)",
            saved_pid, os.getpid()
        )

    keywords = [
        f"decoration:blur:enabled {s['blur']}",
        f"decoration:drop_shadow {s['shadow']}",
        f"animations:enabled {s['anim']}",
    ]

    # Intento 1
    ok = hyprctl_batch(keywords)
    if not ok:
        # Intento 2 tras 1 segundo (Hyprland puede estar ocupado)
        logger.warning("restore_hypr_state: intento 1 fallido, reintentando en 1s...")
        time.sleep(1.0)
        ok = hyprctl_batch(keywords)

    if ok:
        HYPR_STATE_FILE.unlink(missing_ok=True)
        logger.info("Estado Hyprland restaurado")
        return True

    # Ambos intentos fallaron: notificar al usuario de forma visible
    logger.error("restore_hypr_state: FALLÓ tras 2 intentos — estado visual puede estar roto")
    notify(
        "⚠️ natagaming: entorno visual",
        (
            f"No se pudo restaurar blur/sombras/animaciones.\n"
            f"Ejecuta manualmente:\n"
            f"hyprctl keyword animations:enabled {s['anim']}\n"
            f"hyprctl keyword decoration:blur:enabled {s['blur']}"
        ),
        urgency="critical",
        timeout_ms=8000,
    )
    # No borrar el archivo: el próximo inicio puede reintentar
    return False

def apply_gaming_hypr() -> None:
    save_hypr_state()
    hyprctl_batch([
        "decoration:blur:enabled 0",
        "decoration:drop_shadow 0",
        "animations:enabled 0",
    ])
    logger.info("Modo gaming Hyprland activado (blur/shadow/anim off)")

# ── MPV ────────────────────────────────────────────────────────
_MPV_MAX_RESPONSE = 65536

def _mpv_recv_line(s: socket.socket) -> bytes:
    buf = b""
    while b"\n" not in buf:
        if len(buf) > _MPV_MAX_RESPONSE:
            raise ValueError(f"mpv response excedió {_MPV_MAX_RESPONSE} bytes")
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n")[0]

def mpv_cmd(command: dict) -> bool:
    if not MPV_SOCKET.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(str(MPV_SOCKET))
            s.sendall((json.dumps(command) + "\n").encode())
        return True
    except Exception:
        return False

def _mpv_socket_alive() -> bool:
    if not MPV_SOCKET.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(str(MPV_SOCKET))
            s.sendall((json.dumps({"command": ["get_property", "pid"]}) + "\n").encode())
            raw = _mpv_recv_line(s)
            data = json.loads(raw)
            return data.get("error") == "success"
    except Exception:
        return False

def _mpv_cleanup_stale_socket() -> None:
    if MPV_SOCKET.exists() and not _mpv_socket_alive():
        logger.warning("mpv: socket huérfano detectado, limpiando")
        MPV_SOCKET.unlink(missing_ok=True)
        if MPV_PID_FILE.exists():
            try:
                pid = int(MPV_PID_FILE.read_text().strip())
                os.kill(pid, 0)
            except (ProcessLookupError, ValueError):
                MPV_PID_FILE.unlink(missing_ok=True)

_mpv_proc: Optional[subprocess.Popen] = None
import threading as _threading
_mpv_start_lock_sync = _threading.Lock()

_mpv_start_lock_async: Optional[asyncio.Lock] = None

def _get_mpv_lock_async() -> asyncio.Lock:
    global _mpv_start_lock_async
    if _mpv_start_lock_async is None:
        _mpv_start_lock_async = asyncio.Lock()
    return _mpv_start_lock_async

def mpv_start(cfg: Config) -> None:
    global _mpv_proc
    if not cfg.ENABLE_MPV:
        return
    with _mpv_start_lock_sync:
        _mpv_cleanup_stale_socket()
        if _mpv_proc is not None:
            _mpv_proc.poll()
        if _mpv_socket_alive():
            logger.debug("mpv: ya está corriendo y responde")
            return
        _mpv_proc = subprocess.Popen(
            ["mpv", "--no-video", "--idle",
             f"--input-ipc-server={MPV_SOCKET}",
             "--ytdl-format=bestaudio", cfg.PLAYLIST],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        MPV_PID_FILE.write_text(str(_mpv_proc.pid))
        logger.info("mpv iniciado (PID %d)", _mpv_proc.pid)
        for _ in range(15):
            time.sleep(0.2)
            if _mpv_socket_alive():
                break
        else:
            logger.warning("mpv: socket no disponible tras 3s de espera")

async def mpv_start_async(cfg: Config) -> None:
    global _mpv_proc
    if not cfg.ENABLE_MPV:
        return
    async with _get_mpv_lock_async():
        await asyncio.get_running_loop().run_in_executor(None, _mpv_cleanup_stale_socket)
        if _mpv_proc is not None:
            _mpv_proc.poll()
        if await asyncio.get_running_loop().run_in_executor(None, _mpv_socket_alive):
            logger.debug("mpv: ya está corriendo y responde")
            return
        _mpv_proc = subprocess.Popen(
            ["mpv", "--no-video", "--idle",
             f"--input-ipc-server={MPV_SOCKET}",
             "--ytdl-format=bestaudio", cfg.PLAYLIST],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        MPV_PID_FILE.write_text(str(_mpv_proc.pid))
        logger.info("mpv iniciado (PID %d)", _mpv_proc.pid)
        for _ in range(15):
            await asyncio.sleep(0.2)
            if await asyncio.get_running_loop().run_in_executor(None, _mpv_socket_alive):
                break
        else:
            logger.warning("mpv: socket no disponible tras 3s de espera")

# ── FIX 5: mpv_stop — timeout en el wait() post-SIGKILL ──
def mpv_stop() -> None:
    global _mpv_proc
    mpv_cmd({"command": ["quit"]})
    if _mpv_proc is not None:
        try:
            _mpv_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning("mpv: no respondió a quit, matando con SIGKILL")
            _mpv_proc.kill()
            try:
                _mpv_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.error("mpv: proceso zombie, no responde ni a SIGKILL")
        _mpv_proc = None
    elif MPV_PID_FILE.exists():
        try:
            pid = int(MPV_PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            # Pequeña espera y SIGKILL si el proceso sigue vivo
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ProcessLookupError, ValueError):
            pass
    MPV_PID_FILE.unlink(missing_ok=True)
    MPV_SOCKET.unlink(missing_ok=True)

def mpv_pause() -> None:
    mpv_cmd({"command": ["set_property", "pause", True]})

def mpv_resume() -> None:
    mpv_cmd({"command": ["set_property", "pause", False]})

def mpv_get_paused() -> Optional[bool]:
    if not _mpv_socket_alive():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(str(MPV_SOCKET))
            s.sendall((json.dumps({"command": ["get_property", "pause"]}) + "\n").encode())
            raw = _mpv_recv_line(s)
        data = json.loads(raw)
        if data.get("error") == "success":
            return bool(data.get("data"))
    except Exception:
        pass
    return None

# ── Spotify ────────────────────────────────────────────────────
_spotify_player_cache: tuple[Optional[str], float] = (None, 0.0)
_PLAYER_CACHE_TTL = 30.0

def _detect_spotify_player() -> Optional[str]:
    try:
        r = subprocess.run(["playerctl", "-l"],
                           capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            name = line.strip()
            if "spotify" in name.lower():
                return name
    except Exception:
        pass
    return None

def _get_spotify_player(cfg: Config) -> Optional[str]:
    global _spotify_player_cache
    if cfg.SPOTIFY_PLAYER_NAME:
        return cfg.SPOTIFY_PLAYER_NAME
    cached_player, cached_ts = _spotify_player_cache
    if cached_player is not None and (time.monotonic() - cached_ts) < _PLAYER_CACHE_TTL:
        return cached_player
    detected = _detect_spotify_player()
    _spotify_player_cache = (detected, time.monotonic())
    if detected:
        logger.debug("Spotify player detectado y cacheado: %s", detected)
    return detected

def _invalidate_spotify_cache() -> None:
    global _spotify_player_cache
    _spotify_player_cache = (None, 0.0)

def spotify_pause(state: State, cfg: Config) -> None:
    if not cfg.ENABLE_SPOTIFY_PAUSE:
        return
    player = _get_spotify_player(cfg)
    if not player:
        state.spotify_was_playing = False
        return
    try:
        r = subprocess.run(["playerctl", "-p", player, "status"],
                           capture_output=True, text=True, timeout=2)
        if "Playing" in r.stdout:
            state.spotify_was_playing = True
            subprocess.run(["playerctl", "-p", player, "pause"],
                           capture_output=True, timeout=2)
        else:
            state.spotify_was_playing = False
    except Exception:
        state.spotify_was_playing = False

def spotify_resume(state: State, cfg: Config) -> None:
    if not cfg.ENABLE_SPOTIFY_PAUSE:
        return
    if not state.spotify_was_playing:
        return
    player = _get_spotify_player(cfg)
    if not player:
        logger.warning("spotify_resume: player no encontrado (¿cerrado durante gaming?), "
                       "limpiando flag")
        state.spotify_was_playing = False
        return
    try:
        r = subprocess.run(["playerctl", "-p", player, "status"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode != 0 or not r.stdout.strip():
            logger.warning("spotify_resume: player '%s' no responde, limpiando flag", player)
            state.spotify_was_playing = False
            return
        status = r.stdout.strip()
        if status in ("Paused", "Stopped"):
            subprocess.run(["playerctl", "-p", player, "play"],
                           capture_output=True, timeout=2)
            logger.info("Spotify reanudado (%s, estado previo: %s)", player, status)
        else:
            logger.debug("spotify_resume: estado '%s', sin acción", status)
    except Exception as e:
        logger.warning("spotify_resume: error: %s", e)
    finally:
        state.spotify_was_playing = False

# ── CPU Governor ───────────────────────────────────────────────
def save_cpu_governor() -> None:
    gov_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    gov = gov_path.read_text().strip() if gov_path.exists() else "schedutil"
    CPU_STATE_FILE.write_text(gov)

def apply_cpu_performance(cfg: Config) -> None:
    if not cfg.ENABLE_CPU_GOVERNOR:
        return
    save_cpu_governor()
    if shutil.which("powerprofilesctl"):
        try:
            r = subprocess.run(["powerprofilesctl", "set", "performance"],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                logger.info("CPU → performance (powerprofilesctl)")
                return
        except Exception as e:
            logger.warning("powerprofilesctl error: %s", e)
    if shutil.which("cpupower"):
        try:
            r = subprocess.run(
                ["sudo", "-n", "cpupower", "frequency-set", "-g", "performance"],
                capture_output=True, timeout=5)
            if r.returncode == 0:
                logger.info("CPU governor → performance (cpupower)")
            else:
                logger.warning("cpupower falló (¿falta NOPASSWD en sudoers?)")
        except subprocess.TimeoutExpired:
            logger.warning("cpupower timeout")
        except Exception as e:
            logger.warning("cpupower error: %s", e)
    else:
        logger.warning("ENABLE_CPU_GOVERNOR=1 pero no se encontró powerprofilesctl ni cpupower")

def restore_cpu_governor(cfg: Config) -> None:
    if not cfg.ENABLE_CPU_GOVERNOR or not CPU_STATE_FILE.exists():
        return
    gov = CPU_STATE_FILE.read_text().strip()
    restored = False
    if shutil.which("powerprofilesctl"):
        ppc_map = {
            "performance": "performance",
            "powersave":   "power-saver",
            "schedutil":   "balanced",
            "ondemand":    "balanced",
            "conservative":"balanced",
        }
        ppc_gov = ppc_map.get(gov, "balanced")
        try:
            r = subprocess.run(["powerprofilesctl", "set", ppc_gov],
                               capture_output=True, timeout=5)
            restored = r.returncode == 0
            if restored:
                logger.info("CPU → '%s' (powerprofilesctl; governor original '%s')", ppc_gov, gov)
        except Exception:
            pass
    if not restored and shutil.which("cpupower"):
        try:
            r = subprocess.run(
                ["sudo", "-n", "cpupower", "frequency-set", "-g", gov],
                capture_output=True, timeout=5)
            if r.returncode == 0:
                logger.info("CPU governor restaurado → %s (cpupower)", gov)
            else:
                logger.warning("cpupower: no se pudo restaurar governor '%s'", gov)
        except Exception:
            pass
    CPU_STATE_FILE.unlink(missing_ok=True)

# ── Waybar ─────────────────────────────────────────────────────
def _save_waybar_pid() -> None:
    try:
        r = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-x", "waybar"],
            capture_output=True, text=True, timeout=2
        )
        pids = [p.strip() for p in r.stdout.splitlines() if p.strip().isdigit()]
        if pids:
            WAYBAR_PID_FILE.write_text(pids[0])
            logger.debug("waybar PID guardado: %s", pids[0])
    except Exception as e:
        logger.debug("waybar: no se pudo obtener PID: %s", e)

def _send_waybar_signal(sig: int) -> None:
    pid_str = None
    if WAYBAR_PID_FILE.exists():
        pid_str = WAYBAR_PID_FILE.read_text().strip()
    if pid_str and pid_str.isdigit():
        try:
            os.kill(int(pid_str), sig)
            return
        except (ProcessLookupError, PermissionError):
            WAYBAR_PID_FILE.unlink(missing_ok=True)

    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    if not wayland_display:
        logger.warning("_send_waybar_signal: WAYLAND_DISPLAY no definido, skip pkill fallback")
        return
    try:
        r = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-x", "waybar"],
            capture_output=True, text=True, timeout=2
        )
        for pid_s in r.stdout.splitlines():
            if not pid_s.strip().isdigit():
                continue
            pid = int(pid_s.strip())
            try:
                env_bytes = Path(f"/proc/{pid}/environ").read_bytes()
                env_str = env_bytes.replace(b"\x00", b"\n").decode(errors="replace")
                if f"WAYLAND_DISPLAY={wayland_display}" in env_str:
                    os.kill(pid, sig)
            except (PermissionError, FileNotFoundError):
                pass
    except Exception as e:
        logger.warning("waybar signal fallback: %s", e)

def waybar_hide(cfg: Config) -> None:
    if cfg.ENABLE_WAYBAR:
        _save_waybar_pid()
        _send_waybar_signal(signal.SIGUSR1)

def waybar_show(cfg: Config) -> None:
    if cfg.ENABLE_WAYBAR:
        _send_waybar_signal(signal.SIGUSR2)

# ── FIX 4: Window detection — lista vacía no activa gaming mode ──
def is_gaming_window(wclass: str, cfg: Config) -> bool:
    if not wclass:
        return False
    for pat in cfg._compiled_ignore:
        if pat.search(wclass):
            return False
    if not cfg._compiled_gaming:
        # Sin patrones configurados es más seguro no activar gaming mode
        # que activarlo con cualquier ventana en fullscreen
        logger.debug("is_gaming_window: GAMING_WINDOW_CLASSES vacío, retornando False")
        return False
    for pat in cfg._compiled_gaming:
        if pat.search(wclass):
            return True
    return False

def get_fullscreen_class() -> str:
    out = hyprctl("-j", "activewindow")
    if out:
        try:
            data = json.loads(out)
            if data.get("fullscreen") and not data.get("hidden", False):
                wclass = data.get("class", "")
                logger.debug("Fullscreen detectado (activewindow): %s", wclass)
                return wclass
        except (json.JSONDecodeError, AttributeError):
            pass

    clients_out = hyprctl("-j", "clients")
    if clients_out:
        try:
            for client in json.loads(clients_out):
                if not client.get("fullscreen"):
                    continue
                if client.get("hidden", False):
                    continue
                if not client.get("mapped", True):
                    continue
                ws = client.get("workspace", {})
                ws_name = ws.get("name", "") if isinstance(ws, dict) else ""
                if ws_name.startswith("special"):
                    continue
                wclass = client.get("class", "")
                logger.debug("Fullscreen detectado (clients scan): %s", wclass)
                return wclass
        except (json.JSONDecodeError, AttributeError):
            pass

    return ""

# ── Async helpers ──────────────────────────────────────────────
async def _run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)

# ── Suspend/resume detection ───────────────────────────────────
def _get_boottime() -> float:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except AttributeError:
        return time.monotonic()

async def _suspend_monitor(state: State, cfg: Config) -> None:
    last_ts = _get_boottime()
    SUSPEND_DRIFT_THRESHOLD_S = 5.0

    while not state.shutdown_event.is_set():  # type: ignore[union-attr]
        try:
            await asyncio.wait_for(
                state.shutdown_event.wait(),  # type: ignore[union-attr]
                timeout=2.0
            )
            return
        except asyncio.TimeoutError:
            pass

        now = _get_boottime()
        drift = now - last_ts
        last_ts = now

        if drift > SUSPEND_DRIFT_THRESHOLD_S:
            logger.warning(
                "Detect suspend/resume (drift=%.1fs) — reiniciando subsistemas", drift
            )
            await _post_suspend_reinit(state, cfg)

async def _post_suspend_reinit(state: State, cfg: Config) -> None:
    logger.info("Post-suspend reinit...")

    await _run_blocking(_mpv_cleanup_stale_socket)
    _invalidate_spotify_cache()

    if cfg.ENABLE_SPOTIFY_PAUSE and state.spotify_was_playing:
        player = await _run_blocking(_get_spotify_player, cfg)
        if not player:
            logger.warning("post-suspend: Spotify player perdido, limpiando flag")
            state.spotify_was_playing = False

    if state.gaming_active and cfg.ENABLE_MPV:
        alive = await _run_blocking(_mpv_socket_alive)
        if not alive:
            logger.info("post-suspend: mpv murió, reiniciando")
            await mpv_start_async(cfg)

    await asyncio.sleep(1.5)
    await resync_state_async(state, cfg)

    save_daemon_state(state)
    logger.info("Post-suspend reinit completo")

# ── Gaming mode ────────────────────────────────────────────────
async def enter_gaming_mode_async(state: State, cfg: Config) -> None:
    if state.gaming_active:
        return
    if not state.can_transition():
        return
    state.gaming_active = True
    state.mark_transition()
    logger.info("→ MODO GAMING activado")
    notify("🎮 Gaming mode", "ON", timeout_ms=cfg.NOTIFY_TIMEOUT)
    await _run_blocking(apply_gaming_hypr)
    await _run_blocking(apply_cpu_performance, cfg)
    await _run_blocking(spotify_pause, state, cfg)
    await mpv_start_async(cfg)
    paused = await _run_blocking(mpv_get_paused)
    state.mpv_was_playing = (paused is False)
    await _run_blocking(waybar_hide, cfg)
    save_daemon_state(state)

async def exit_gaming_mode_async(state: State, cfg: Config) -> None:
    if not state.gaming_active:
        return
    if not state.can_transition():
        return
    state.gaming_active = False
    state.mark_transition()
    logger.info("← MODO GAMING desactivado")
    notify("🎮 Gaming mode", "OFF", timeout_ms=cfg.NOTIFY_TIMEOUT)
    await _run_blocking(restore_hypr_state)
    await _run_blocking(restore_cpu_governor, cfg)
    await _run_blocking(spotify_resume, state, cfg)
    if state.mpv_was_playing:
        await _run_blocking(mpv_resume)
    else:
        await _run_blocking(mpv_pause)
    state.mpv_was_playing = False
    await _run_blocking(waybar_show, cfg)
    save_daemon_state(state)

def enter_gaming_mode(state: State, cfg: Config) -> None:
    if state.gaming_active:
        return
    state.gaming_active = True
    logger.info("→ MODO GAMING activado")
    notify("🎮 Gaming mode", "ON", timeout_ms=cfg.NOTIFY_TIMEOUT)
    apply_gaming_hypr()
    apply_cpu_performance(cfg)
    spotify_pause(state, cfg)
    mpv_start(cfg)
    paused = mpv_get_paused()
    state.mpv_was_playing = (paused is False)
    waybar_hide(cfg)
    save_daemon_state(state)

def exit_gaming_mode(state: State, cfg: Config) -> None:
    if not state.gaming_active:
        return
    state.gaming_active = False
    logger.info("← MODO GAMING desactivado")
    notify("🎮 Gaming mode", "OFF", timeout_ms=cfg.NOTIFY_TIMEOUT)
    restore_hypr_state()
    restore_cpu_governor(cfg)
    spotify_resume(state, cfg)
    if state.mpv_was_playing:
        mpv_resume()
    else:
        mpv_pause()
    state.mpv_was_playing = False
    waybar_show(cfg)
    save_daemon_state(state)

def run_with_gaming_mode(state: State, cfg: Config, args: list[str]) -> int:
    enter_gaming_mode(state, cfg)
    try:
        r = subprocess.run(args)
        return r.returncode
    finally:
        exit_gaming_mode(state, cfg)

# ── FIX 3: cleanup — restaurar entorno visual ANTES de mpv_stop ──
def cleanup(state: State, cfg: Config) -> None:
    if state.cleaned:
        return
    state.cleaned = True
    logger.info("Limpiando y saliendo...")

    if state.debounce_task and not state.debounce_task.done():
        state.debounce_task.cancel()

    # Visual primero: rápido y crítico para el usuario
    if HYPR_STATE_FILE.exists():
        s = load_hypr_state()
        current_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        owns_state = (s.get("pid") == os.getpid() and s.get("sig") == current_sig)
        if owns_state:
            try:
                restore_hypr_state()
            except Exception as e:
                logger.warning("cleanup: restore_hypr_state falló: %s", e)
                HYPR_STATE_FILE.unlink(missing_ok=True)
        else:
            logger.warning("cleanup: HYPR_STATE_FILE de otra instancia, no restaurando")

    if CPU_STATE_FILE.exists():
        try:
            restore_cpu_governor(cfg)
        except Exception as e:
            logger.warning("cleanup: restore_cpu_governor falló: %s", e)
            CPU_STATE_FILE.unlink(missing_ok=True)

    # mpv al final: puede tardar o bloquearse, no es crítico para el entorno visual
    try:
        mpv_stop()
    except Exception:
        pass

    for f in (PID_FILE, MPV_SOCKET, MPV_PID_FILE, WAYBAR_PID_FILE):
        f.unlink(missing_ok=True)
    clear_daemon_state()
    release_lock()

# ── Resync ─────────────────────────────────────────────────────
async def resync_state_async(state: State, cfg: Config) -> None:
    wclass  = await _run_blocking(get_fullscreen_class)
    is_game = is_gaming_window(wclass, cfg)
    if is_game == state.gaming_active:
        return
    if is_game:
        await enter_gaming_mode_async(state, cfg)
    else:
        await exit_gaming_mode_async(state, cfg)

# ── Debounce ───────────────────────────────────────────────────
def schedule_debounce(state: State, cfg: Config) -> None:
    if state.debounce_task and not state.debounce_task.done():
        state.debounce_task.cancel()
    state.debounce_task = asyncio.create_task(_debounce_resync(state, cfg))

async def _debounce_resync(state: State, cfg: Config) -> None:
    try:
        await asyncio.sleep(cfg.DEBOUNCE_MS / 1000)
        await resync_state_async(state, cfg)
    except asyncio.CancelledError:
        pass

# ── Event loop ─────────────────────────────────────────────────
async def event_loop(state: State, cfg: Config) -> None:
    sock_path = get_socket_path()
    hypr_dir = sock_path.parent
    logger.info("Conectando al socket Hyprland: %s", sock_path)

    INTERESTING = ("fullscreen>>", "activewindow>>", "closewindow>>", "workspace>>")
    backoff = 2.0
    _last_warn_ts = 0.0
    _consecutive_failures = 0
    _MAX_CONSECUTIVE_FAILURES = 10

    assert state.shutdown_event is not None
    while not state.shutdown_event.is_set():
        if not hypr_dir.exists():
            logger.warning(
                "Directorio Hyprland '%s' desaparecido — sesión terminada, iniciando shutdown",
                hypr_dir
            )
            state.shutdown_event.set()
            return

        await resync_state_async(state, cfg)
        try:
            reader, _ = await asyncio.open_unix_connection(str(sock_path))
            logger.info("Conectado al socket Hyprland")
            backoff = 2.0
            _last_warn_ts = 0.0
            _consecutive_failures = 0
            async for line_bytes in reader:
                if state.shutdown_event.is_set():
                    return
                line = line_bytes.decode(errors="replace").strip()
                if any(line.startswith(ev) for ev in INTERESTING):
                    logger.debug("Evento: %s", line)
                    schedule_debounce(state, cfg)
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            _consecutive_failures += 1

        if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "Socket Hyprland inaccesible tras %d intentos consecutivos — shutdown",
                _consecutive_failures
            )
            state.shutdown_event.set()
            return

        now = asyncio.get_event_loop().time()
        if now - _last_warn_ts >= 30.0:
            logger.warning("Socket Hyprland cerrado; reintentando (backoff=%.0fs)...", backoff)
            _last_warn_ts = now
        try:
            await asyncio.wait_for(state.shutdown_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 60.0)

# ── Modes ──────────────────────────────────────────────────────
def mode_auto(state: State, cfg: Config) -> None:
    try:
        asyncio.run(_run_event_loop(state, cfg))
    finally:
        cleanup(state, cfg)

async def _run_event_loop(state: State, cfg: Config) -> None:
    state.shutdown_event = asyncio.Event()
    setup_signals(state, cfg, CONFIG_FILE)

    suspend_task = asyncio.create_task(_suspend_monitor(state, cfg))
    try:
        await event_loop(state, cfg)
    finally:
        suspend_task.cancel()
        try:
            await suspend_task
        except asyncio.CancelledError:
            pass

def mode_steam(state: State, cfg: Config, args: list[str]) -> None:
    if not shutil.which("steam"):
        logger.error("mode steam: 'steam' no encontrado en PATH")
        sys.exit(1)
    game_id = args[0] if args else ""
    url = f"steam://rungameid/{game_id}" if game_id else "steam://"
    subprocess.Popen(["steam", url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
    mode_auto(state, cfg)

def mode_run(state: State, cfg: Config, args: list[str]) -> None:
    if not args:
        logger.error("mode run: necesita un comando")
        sys.exit(1)
    run_with_gaming_mode(state, cfg, args)

def mode_gamescope(state: State, cfg: Config, args: list[str]) -> None:
    if not args:
        logger.error("mode gamescope: necesita un comando")
        sys.exit(1)
    if not shutil.which("gamescope"):
        logger.error("mode gamescope: 'gamescope' no encontrado en PATH")
        sys.exit(1)
    res_w, res_h = cfg.GAMESCOPE_RES.split("x")
    scope_args = [
        "gamescope",
        "-W", res_w, "-H", res_h, "-r", str(cfg.GAMESCOPE_HZ),
        *cfg.GAMESCOPE_FLAGS,
        "--", *args,
    ]
    run_with_gaming_mode(state, cfg, scope_args)

def mode_wine(state: State, cfg: Config, args: list[str]) -> None:
    if not args:
        logger.error("mode wine: necesita un ejecutable")
        sys.exit(1)
    run_with_gaming_mode(state, cfg, ["wine", *args])

def mode_proton(state: State, cfg: Config, args: list[str]) -> None:
    if not args:
        logger.error("mode proton: necesita un ejecutable")
        sys.exit(1)

    proton_path = cfg.PROTON_PATH
    if not proton_path:
        common = Path.home() / ".steam/root/steamapps/common"

        def _proton_sort_key(p: Path) -> tuple:
            name  = p.parent.name
            lower = name.lower()
            if "experimental" in lower:
                return (0, 0, 0)
            if "hotfix" in lower:
                return (1, 0, 0)
            parts = re.findall(r"\d+", name)
            try:
                return (2, *[int(x) for x in parts])
            except (ValueError, TypeError):
                return (2, 0)

        candidates = sorted(common.glob("*/proton"), key=_proton_sort_key)
        candidates = [p for p in candidates if p.is_file() and os.access(p, os.X_OK)]
        if not candidates:
            logger.error("No se encontró Proton. Configura PROTON_PATH.")
            sys.exit(1)
        proton_path = str(candidates[-1])
        logger.info("Proton autodetectado: %s", proton_path)

    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"]           = cfg.STEAM_COMPAT_DATA_PATH
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = cfg.STEAM_COMPAT_CLIENT_INSTALL_PATH

    enter_gaming_mode(state, cfg)
    try:
        subprocess.run([proton_path, "run", *args], env=env)
    finally:
        exit_gaming_mode(state, cfg)

def mode_tty(state: State, cfg: Config) -> None:
    for entry in cfg.TTY_APPS:
        parts = entry.split(":", 2)
        if len(parts) != 3:
            logger.warning("TTY_APPS: entrada malformada '%s'", entry)
            continue
        name, tty_str, cmd = parts
        if not tty_str.isdigit():
            logger.warning("TTY_APPS: tty inválida en '%s'", entry)
            continue
        tty_num = int(tty_str)
        cmd_args = shlex.split(cmd)
        logger.info("Lanzando %s en TTY%d", name, tty_num)
        try:
            subprocess.Popen(
                ["openvt", "-c", str(tty_num), "-s", "-w", "--"] + cmd_args,
                start_new_session=True, close_fds=True,
            )
        except Exception as e:
            logger.warning("TTY_APPS: error lanzando '%s': %s", name, e)
    mode_auto(state, cfg)

# ── Signal handling ────────────────────────────────────────────
def setup_signals(state: State, cfg: Config, config_path: Path) -> None:
    loop = asyncio.get_event_loop()

    def _reload():
        logger.info("Recargando config (SIGHUP)...")
        cfg.load(config_path)
        setup_logging(cfg.LOG_LEVEL)
        check_deps(cfg)
        logger.info("Config recargada OK")
        notify("natagaming", "Config recargada", timeout_ms=cfg.NOTIFY_TIMEOUT)

    def _shutdown():
        logger.info("Señal recibida, iniciando shutdown limpio...")
        if state.shutdown_event is not None:
            state.shutdown_event.set()

    loop.add_signal_handler(signal.SIGHUP,  _reload)
    loop.add_signal_handler(signal.SIGTERM, _shutdown)
    loop.add_signal_handler(signal.SIGINT,  _shutdown)

# ── Entry point ────────────────────────────────────────────────
def main() -> None:
    acquire_lock()

    cfg = Config()
    cfg.load(CONFIG_FILE)
    setup_logging(cfg.LOG_LEVEL)
    check_deps(cfg)

    state = State()

    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    args = sys.argv[2:]

    modes = {
        "auto":      lambda: mode_auto(state, cfg),
        "steam":     lambda: mode_steam(state, cfg, args),
        "run":       lambda: mode_run(state, cfg, args),
        "gamescope": lambda: mode_gamescope(state, cfg, args),
        "wine":      lambda: mode_wine(state, cfg, args),
        "proton":    lambda: mode_proton(state, cfg, args),
        "tty":       lambda: mode_tty(state, cfg),
    }

    if mode not in modes:
        print(f"Uso: natagaming.py [{'|'.join(modes)}] [args...]", file=sys.stderr)
        sys.exit(1)

    modes[mode]()

if __name__ == "__main__":
    main()
