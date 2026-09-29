#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJK Ublox GPS Board - Linux-side main program (Arduino UNO Q / App Lab, runs in container)

Responsibilities:
  1) Connect to an NTRIP server, pull correction data, forward it to the MCU over the bridge
  2) Parse NMEA coming back from the X20P module and report position every gga_interval seconds
  3) Serve a web configuration UI; persist credentials to config.json

Before changing anything, read this:
  * NtripWorker runs exactly one long-lived supervisor thread. A config change does not
    spawn a thread - it bumps an epoch and sets _stop, and the supervisor reconnects with
    the new config. This makes it structurally impossible for two threads to hold a socket.
  * _stop.clear() must happen inside the _lock critical section that reads the epoch.
    Outside the lock it can wipe the signal sent by set_config, leaving the worker running
    the old config forever without reporting an error.
  * The web layer is optional decoration. If the WebUI brick fails to import, still start
    the worker and still call App.run() - App.run() is what drives the bridge, so skipping
    it kills the whole application.

Credential safety: config.json lives at /app inside the container (the host application
directory) and is bundled into the zip produced by App Lab's export feature. Clear the
credentials from the UI before sharing the app.
"""

import atexit
import base64
import collections
import contextlib
import json
import os
import signal
import socket
import tempfile
import threading
import time

from arduino.app_utils import Bridge, App

# The WebUI brick is an optional dependency: its absence must not prevent startup.
try:
    from arduino.app_bricks.web_ui import WebUI

    WEBUI_AVAILABLE = True
    WEBUI_IMPORT_ERROR = None
except ImportError as _e:  # noqa: BLE001 - report the reason to the user verbatim
    WEBUI_AVAILABLE = False
    WEBUI_IMPORT_ERROR = str(_e)


# ============================== Constants and defaults ==============================

APP_NAME = "MJK Ublox GPS Board"
APP_VERSION = "1.0.0"
START_TIME = time.time()

SIGNUP_URL = "https://portal.thingstream.io/register"

SPARTN_CHUNK = 32        # bridge caps a message at ~96 chars; 32 bytes = 64 hex chars, do not raise
CONNECT_TIMEOUT = 5.0    # TCP connect timeout, seconds
HEADER_TIMEOUT = 15.0    # total time allowed for the response header, seconds
SOCK_TIMEOUT = 1.0       # socket recv timeout, seconds - sets how fast cancellation reacts
GGA_INTERVAL_RANGE = (1.0, 30.0)   # position report interval range; above 30 s the server drops us

# PointPerfect defaults, pre-filled. Every field is editable in the UI.
DEFAULTS = {
    "host": "ppntrip.services.u-blox.com",
    "port": 2101,                 # 2101 without TLS; 2102 with TLS
    "mountpoint": "NEAR-SPARTN",
    "username": "",
    "password": "",
    "bootstrap_lat": 0.0,         # fallback position before the first fix; 0,0 raises a UI warning
    "bootstrap_lon": 0.0,
    "gga_interval": 5.0,
    "auto_start": True,
}

# NMEA GGA position quality (field 6)
QUALITY_NAMES = {
    0: "invalid", 1: "SPS", 2: "DGPS", 3: "PPS",
    4: "RTK-FIXED", 5: "RTK-FLOAT", 6: "EST", 7: "MANUAL", 8: "SIM",
}
# Qualities good enough to report a position with: SPS and above. The mountpoint picks the
# nearest base station from the reported position, and an autonomous fix is accurate enough
# for that, while being available seconds after power-on.
GGA_USABLE_QUALITY = (1, 2, 3, 4, 5)

_BRIDGE_LOCK = threading.Lock()   # serialise bridge calls


# ============================== Log ring buffer ==============================
# Writes to stdout (for `arduino-app-cli app logs`) and to an in-memory ring buffer
# (for the run-log panel in the UI, keeping the most recent 200 lines).


class LogRing:
    def __init__(self, capacity=200):
        self._buf = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, level, msg):
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "t": time.time(), "level": level, "msg": msg}
            self._buf.append(entry)
        print(f"[{level}] {msg}", flush=True)
        return entry["seq"]

    def snapshot(self, since=0, limit=100):
        with self._lock:
            out = [e for e in self._buf if e["seq"] > since]
        return out[-limit:]

    def next_seq(self):
        with self._lock:
            return self._seq + 1


log = LogRing()


# ============================== NMEA helpers ==============================


def nmea_checksum(body: str) -> str:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"{c:02X}"


def dd_to_dmm(deg: float, pos: str, neg: str):
    hemi = pos if deg >= 0 else neg
    deg = abs(deg)
    d = int(deg)
    m = (deg - d) * 60.0
    return f"{d:02d}{m:07.4f}", hemi


def make_gga(lat: float, lon: float, alt: float = 0.0) -> str:
    t = time.gmtime()
    lat_s, lat_h = dd_to_dmm(lat, "N", "S")
    lon_s, lon_h = dd_to_dmm(lon, "E", "W")
    body = (f"GNGGA,{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}.00,"
            f"{lat_s},{lat_h},{lon_s},{lon_h},1,12,1.0,{alt:.1f},M,0.0,M,,")
    return f"${body}*{nmea_checksum(body)}"


def parse_gga(line: str):
    f = line.split(",")
    if len(f) < 10 or not f[0].endswith("GGA"):
        return None

    def dm_to_deg(v, hemi):
        v = float(v)
        d = int(v / 100) + (v - int(v / 100) * 100) / 60.0
        return -d if hemi in ("S", "W") else d

    try:
        quality = int(f[6]) if f[6] else 0
        has_position = bool(f[2]) and bool(f[4])   # distinguishes "empty field" from "really at 0,0"
        lat = dm_to_deg(f[2], f[3]) if has_position else 0.0
        lon = dm_to_deg(f[4], f[5]) if has_position else 0.0
        num_sv = int(f[7]) if f[7] else 0
        hdop = float(f[8]) if f[8] else 0.0
        alt = float(f[9]) if f[9] else 0.0
    except ValueError:
        return None
    return {
        "quality": QUALITY_NAMES.get(quality, str(quality)),
        "quality_code": quality,
        "has_position": has_position,
        "lat": lat, "lon": lon, "alt": alt,
        "numSV": num_sv, "hdop": hdop,
    }


# ============================== HTTP chunked decoding ==============================


class ChunkReader:
    """Decodes chunked transfer encoding; passes data through untouched if not chunked."""

    def __init__(self):
        self.buf = b""
        self.raw = False

    def feed(self, data: bytes) -> bytes:
        if self.raw:
            return data
        if data:
            self.buf += data
        if not self.buf:
            return b""
        c = self.buf[0]
        if not (0x30 <= c <= 0x39 or 0x61 <= c <= 0x66 or 0x41 <= c <= 0x46):
            # first byte is not a hex digit => not chunked
            self.raw = True
            out = bytes(self.buf)
            self.buf = b""
            return out
        out = b""
        while True:
            idx = self.buf.find(b"\r\n")
            if idx < 0:
                break
            try:
                size = int(self.buf[:idx], 16)
            except ValueError:
                self.raw = True
                out += bytes(self.buf)
                self.buf = b""
                break
            if size == 0:
                self.raw = True
                self.buf = b""
                break
            need = idx + 2 + size + 2
            if len(self.buf) < need:
                break
            out += self.buf[idx + 2:idx + 2 + size]
            self.buf = self.buf[need:]
        return out


# ============================== Configuration persistence ==============================


class ConfigError(ValueError):
    """Configuration validation or save failure (bad user input; surfaced as a message)."""


def resolve_config_path():
    """Returns (config path, reason). The reason is shown in the UI to help diagnose
    "my settings were not saved" reports.

    Inside the container APP_HOME points at a host path that does not exist in the
    container namespace, so this falls through to /app, which is mounted from the host
    application directory and therefore persists.
    """
    env = os.environ.get("PP_CONFIG_PATH")
    if env:
        return env, "PP_CONFIG_PATH environment variable"

    home = os.environ.get("APP_HOME")
    if home and os.path.isdir(home) and os.access(home, os.W_OK):
        return os.path.join(home, "config.json"), "APP_HOME (host path)"

    if os.path.isdir("/app") and os.access("/app", os.W_OK):
        return "/app/config.json", "container bind mount"

    if home:
        log.add("warn", f"APP_HOME={home} missing or not writable; falling back to script dir")
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "config.json"), "script directory (degraded, may not persist)"


def _coerce_float(v, lo, hi, name, warnings, clamp=True):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number") from None
    if f < lo or f > hi:
        if not clamp:
            raise ConfigError(f"{name} must be between {lo} and {hi}")
        f = max(lo, min(hi, f))
        warnings.append(f"{name} out of range, clamped to {f}")
    return f


def _coerce_int(v, lo, hi, name):
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer") from None
    if i < lo or i > hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return i


def validate_patch(patch, clamp):
    """Validates user-supplied fields. clamp=True for reading from disk (lenient),
    False for API input (strict)."""
    warnings = []
    out = {}

    if "host" in patch:
        host = str(patch["host"] or "").strip()
        for scheme in ("https://", "http://"):
            if host.lower().startswith(scheme):
                # users often paste a browser URL straight in; strip the scheme for them
                host = host[len(scheme):]
                warnings.append("stripped the http:// prefix from the address")
        host = host.split("/")[0].strip()
        if not host:
            raise ConfigError("server address cannot be empty")
        if ":" in host:
            # a pasted host:port - split it apart
            h, _, p = host.partition(":")
            host = h
            if p.isdigit() and "port" not in patch:
                patch = {**patch, "port": int(p)}
                warnings.append(f"detected port {p} in the address")
        out["host"] = host

    if "port" in patch:
        out["port"] = _coerce_int(patch["port"], 1, 65535, "port")

    if "mountpoint" in patch:
        mp = str(patch["mountpoint"] or "").strip().lstrip("/")
        if not mp:
            raise ConfigError("mountpoint cannot be empty")
        out["mountpoint"] = mp

    if "username" in patch:
        out["username"] = str(patch["username"] or "").strip()

    if "password" in patch:
        out["password"] = str(patch["password"] or "")

    if "bootstrap_lat" in patch:
        out["bootstrap_lat"] = _coerce_float(patch["bootstrap_lat"], -90.0, 90.0,
                                             "bootstrap latitude", warnings, clamp)
    if "bootstrap_lon" in patch:
        out["bootstrap_lon"] = _coerce_float(patch["bootstrap_lon"], -180.0, 180.0,
                                             "bootstrap longitude", warnings, clamp)

    if "gga_interval" in patch:
        out["gga_interval"] = _coerce_float(
            patch["gga_interval"], *GGA_INTERVAL_RANGE, "GGA interval", warnings, clamp)

    if "auto_start" in patch:
        out["auto_start"] = bool(patch["auto_start"])

    # Three-state password handling, so "leave unchanged" and "clear it" are distinguishable.
    # WARNING: this must be guarded with `in`. A config read back from disk has no
    # password_action key at all; running the pop below unconditionally would discard the
    # stored password and silently break persistence across restarts.
    if "password_action" in patch:
        action = patch.get("password_action")
        if action == "clear":
            out["password"] = ""
        elif action in (None, "", "keep"):
            out.pop("password", None)
        elif action != "set":
            raise ConfigError("password_action must be keep / set / clear")

    return out, warnings


class ConfigStore:
    """Reads and writes config.json. load() never raises: on failure it falls back to
    defaults and leaves the user's file untouched."""

    DEFAULTS = DEFAULTS

    def __init__(self, path, reason):
        self.path = path
        self.path_reason = reason
        self._lock = threading.Lock()
        self._cfg = dict(self.DEFAULTS)
        self.load_error = None
        self.writable = os.access(os.path.dirname(path) or ".", os.W_OK)
        if not self.writable:
            log.add("warn", f"config directory is not writable: {os.path.dirname(path)}")

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("config root must be an object")
            clean, warnings = validate_patch(raw, clamp=True)
            for w in warnings:
                log.add("warn", f"config: {w}")
            with self._lock:
                self._cfg = {**self.DEFAULTS, **clean}
                self.load_error = None
        except FileNotFoundError:
            with self._lock:
                self._cfg = dict(self.DEFAULTS)   # first run
                self.load_error = None
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # Do not overwrite the user's broken file; fall back to defaults and say so.
            with self._lock:
                self._cfg = dict(self.DEFAULTS)
                self.load_error = str(e)
            log.add("error", f"failed to read config.json ({e}); using defaults, file left as-is")
        return self.get()

    def get(self):
        with self._lock:
            return dict(self._cfg)

    def get_public(self):
        cfg = self.get()
        has_pwd = bool(cfg.get("password"))
        cfg["password"] = ""                      # never return the password
        cfg["password_set"] = has_pwd
        cfg["config_complete"] = config_complete(cfg)
        return cfg

    def save(self, patch):
        """Validate -> atomic write -> update memory. Returns (public config, warnings)."""
        current = self.get()
        clean, warnings = validate_patch(patch, clamp=False)
        new = {**current, **clean}
        if not self.writable:
            warnings.append("config directory is not writable; settings apply to this "
                            "session only and will be lost on restart")
        else:
            self._atomic_write(json.dumps(new, indent=2, ensure_ascii=False))
            self._sweep_tmp_files()
        with self._lock:
            self._cfg = new
            self.load_error = None
        return self.get_public(), warnings

    def _atomic_write(self, text):
        d = os.path.dirname(self.path) or "."
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())     # matters on SD/eMMC if power is cut
            os.replace(tmp, self.path)   # atomic within the same filesystem
            tmp = None
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)   # it holds a password
        except OSError as e:
            raise ConfigError(f"cannot write {self.path}: {e}") from e
        finally:
            if tmp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def _sweep_tmp_files(self):
        """Remove leftover temp files from an interrupted save (older than one hour)."""
        d = os.path.dirname(self.path) or "."
        cutoff = time.time() - 3600
        with contextlib.suppress(OSError):
            for name in os.listdir(d):
                if name.startswith(".config-") and name.endswith(".tmp"):
                    p = os.path.join(d, name)
                    with contextlib.suppress(OSError):
                        if os.path.getmtime(p) < cutoff:
                            os.unlink(p)


def config_complete(cfg):
    """Credentials count as usable only when complete. Otherwise the worker stays stopped
    instead of hammering the caster with an empty password."""
    return bool(cfg.get("host") and cfg.get("mountpoint")
                and cfg.get("username") and cfg.get("password"))


# ============================== State containers ==============================


class GnssState:
    """X20P positioning results.

    Everything is recorded unconditionally (including invalid / 0,0); the validity filter
    lives only in best_gga(). That way the UI can tell "no antenna" apart from "app hung".
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._line = None      # last NMEA line usable for reporting (with checksum, no newline)
        self._parsed = None
        self.lines_seen = 0
        self.parse_failures = 0
        self.callback_errors = 0
        self.t_last = 0.0

    def submit(self, parsed, raw):
        with self._lock:
            self.lines_seen += 1
            self._parsed = parsed
            self.t_last = time.time()
            if (parsed["quality_code"] in GGA_USABLE_QUALITY
                    and parsed["has_position"]):
                self._line = raw

    def best_gga(self, bootstrap):
        with self._lock:
            if self._line:
                return self._line, "module"
        return bootstrap, "bootstrap"

    def snapshot(self):
        with self._lock:
            p = self._parsed
            return {
                "quality": (p or {}).get("quality", "invalid"),
                "quality_code": (p or {}).get("quality_code", 0),
                "valid": bool(p and p["quality_code"] in GGA_USABLE_QUALITY
                              and p["has_position"]),
                "lat": (p or {}).get("lat"),
                "lon": (p or {}).get("lon"),
                "alt": (p or {}).get("alt"),
                "numSV": (p or {}).get("numSV", 0),
                "hdop": (p or {}).get("hdop", 0.0),
                "age": round(time.time() - self.t_last, 1) if self.t_last else None,
                "lines_seen": self.lines_seen,
                "parse_failures": self.parse_failures,
                "callback_errors": self.callback_errors,
            }


class Status:
    """Link and throughput counters. bytes_per_sec is an EMA computed on each snapshot."""

    def __init__(self):
        self._lock = threading.Lock()
        self._d = {
            "ntrip_state": "stopped",     # stopped|connecting|connected|reconnecting|error
            "epoch": 0,
            "connected_since": None,
            "last_error": None,
            "host": "", "port": 0, "mountpoint": "",
            "bytes_total": 0,
            "push_errors": 0,
            "reconnects": 0,
            "gga_sent": 0,
            "last_gga_sent": None,
            "gga_source": "-",
            "last_byte": None,
            "awaiting_data": True,
        }
        self._rate = (0.0, 0, None)

    def set(self, **kw):
        with self._lock:
            self._d.update(kw)

    def bump(self, key, n=1):
        with self._lock:
            self._d[key] = self._d.get(key, 0) + n

    def get(self, key, default=None):
        with self._lock:
            return self._d.get(key, default)

    def snapshot(self):
        now = time.time()
        with self._lock:
            lt, lb, ema = self._rate
            if lt and now > lt:
                inst = (self._d["bytes_total"] - lb) / (now - lt)
                ema = inst if ema is None else ema * 0.6 + inst * 0.4
            self._rate = (now, self._d["bytes_total"], ema)
            d = dict(self._d)

        def age(key):
            t = d.get(key)
            return round(now - t, 1) if t else None

        d["bytes_per_sec"] = round(ema or 0.0, 1)
        d["last_byte_age"] = age("last_byte")
        d["last_gga_sent_age"] = age("last_gga_sent")
        d["connected_for"] = (round(now - d["connected_since"], 1)
                              if d["connected_since"] else None)
        return d


# ============================== NTRIP worker ==============================


def friendly_error(e):
    if isinstance(e, RuntimeError):
        return str(e)
    if isinstance(e, socket.gaierror):
        return f"DNS lookup failed: {e}"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused - the port may be wrong"
    if isinstance(e, socket.timeout):
        return "network timeout"
    if isinstance(e, OSError):
        return f"network error: {e}"
    return f"{type(e).__name__}: {e}"


class NtripWorker:
    """One long-lived supervisor thread plus an epoch counter.

    A config change does not spawn a thread: it bumps the epoch and sets _stop, and the
    supervisor reconnects with the new config. Two threads holding a socket at once is
    therefore structurally impossible.
    """

    def __init__(self, gnss, status):
        self._gnss = gnss
        self._status = status
        self._lock = threading.Lock()
        self._cfg = None
        self._epoch = 0
        self._enabled = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._sock = None
        self._thread = None

    # ---------- lifecycle ----------

    def start(self):
        self._thread = threading.Thread(target=self._supervisor, name="ntrip",
                                        daemon=True)
        self._thread.start()

    def set_config(self, cfg):
        with self._lock:
            self._cfg = dict(cfg)
            self._epoch += 1
            self._enabled = bool(cfg.get("auto_start", True)) and config_complete(cfg)
            self._stop.set()
            self._wake.set()
            return self._epoch

    def set_enabled(self, enabled):
        with self._lock:
            if enabled and not config_complete(self._cfg or {}):
                raise ConfigError("fill in the server address, mountpoint, username "
                                  "and password first")
            self._enabled = enabled
            self._stop.set()
            self._wake.set()
            return self._epoch

    def is_enabled(self):
        with self._lock:
            return self._enabled

    def epoch(self):
        with self._lock:
            return self._epoch

    def shutdown(self, timeout=3.0):
        """Close cleanly. Without this the caster may keep the mountpoint reserved for
        this credential, which makes the next start look like an authentication failure."""
        self._shutdown.set()
        with self._lock:
            self._stop.set()
            self._wake.set()
        self._close_socket()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def _close_socket(self):
        with self._lock:
            s, self._sock = self._sock, None
        if s is None:
            return
        with contextlib.suppress(OSError):
            s.shutdown(socket.SHUT_RDWR)   # wakes a blocked recv immediately
        with contextlib.suppress(OSError):
            s.close()

    # ---------- supervisor ----------

    def _supervisor(self):
        backoff = 1.0
        while not self._shutdown.is_set():
            with self._lock:
                cfg, epoch, enabled = self._cfg, self._epoch, self._enabled
                # vvv MUST stay inside the lock. Outside it, this wipes the _stop signal
                #     set by set_config and the worker runs the old config forever,
                #     without reporting anything.
                self._stop.clear()

            if not enabled or cfg is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue

            self._status.set(ntrip_state="connecting", last_error=None, epoch=epoch,
                             host=cfg["host"], port=cfg["port"],
                             mountpoint=cfg["mountpoint"])
            try:
                self._pump(cfg, epoch)
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - every failure becomes a UI message
                msg = friendly_error(e)
                log.add("error", f"[ntrip] {msg}")
                self._status.set(ntrip_state="error", last_error=msg)
            finally:
                self._close_socket()

            if self._shutdown.is_set():
                break
            with self._lock:
                changed = self._epoch != epoch
                enabled = self._enabled
            if changed:
                continue                      # config changed, reconnect right away
            if not enabled:
                self._status.set(ntrip_state="stopped")
                continue

            self._status.set(ntrip_state="reconnecting")
            self._status.bump("reconnects")
            if self._wake.wait(backoff):      # interruptible by a config change
                self._wake.clear()
                backoff = 1.0
            else:
                backoff = min(backoff * 2.0, 30.0)

    # ---------- lifetime of one connection ----------

    def _pump(self, cfg, epoch):
        sock, rest = self._connect(cfg)
        with self._lock:
            self._sock = sock

        self._status.set(ntrip_state="connected", connected_since=time.time(),
                         awaiting_data=True, last_error=None)
        log.add("info", f"[ntrip] connected to {cfg['host']}:{cfg['port']}/{cfg['mountpoint']}")

        cr = ChunkReader()
        if rest:
            payload = cr.feed(rest)           # data right after the header must not be dropped
            if payload:
                self._push(payload)

        self._send_gga(sock, cfg)             # the caster only starts streaming after a GGA
        last_gga = last_report = time.time()
        silent_since = time.time()
        got_first = False
        warned_silent = False

        while not self._stop.is_set():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                pass                          # a normal 1 Hz heartbeat, not an error
            except OSError:
                if self._stop.is_set():
                    return
                raise
            else:
                if not data:
                    log.add("warn", "[ntrip] the caster closed the connection")
                    return
                payload = cr.feed(data)
                if payload:
                    if not got_first:
                        got_first = True
                        self._status.set(awaiting_data=False)
                    self._push(payload)

            now = time.time()
            if not got_first and not warned_silent and now - silent_since > 20:
                warned_silent = True
                log.add("warn", "[ntrip] connected but no data for 20 s - the mountpoint may "
                                "not cover your position; check the GGA coordinates or try "
                                "another mountpoint")
            if now - last_gga >= cfg["gga_interval"]:
                if self._send_gga(sock, cfg):
                    self._status.bump("gga_sent")
                last_gga = now
            if now - last_report >= 10.0:
                log.add("info", f"[ntrip] pushed {self._status.get('bytes_total', 0)} bytes total")
                last_report = now

    def _connect(self, cfg):
        host, port = cfg["host"], int(cfg["port"])
        log.add("info", f"[ntrip] connecting to {host}:{port} ...")
        try:
            sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except socket.gaierror as e:
            raise RuntimeError(f"DNS lookup failed: {host} ({e})") from e
        except ConnectionRefusedError as e:
            raise RuntimeError(f"connection refused: {host}:{port} - the port may be wrong") from e
        except socket.timeout as e:
            raise RuntimeError(f"connect timeout ({CONNECT_TIMEOUT:.0f} s): {host}:{port}") from e
        except OSError as e:
            raise RuntimeError(f"network unreachable: {e}") from e

        # The timeout must be set before reading the header, otherwise a server that
        # accepts the connection and then goes quiet blocks until the connect timeout.
        sock.settimeout(SOCK_TIMEOUT)
        try:
            auth = base64.b64encode(
                f"{cfg['username']}:{cfg['password']}".encode()).decode()
            req = (f"GET /{cfg['mountpoint']} HTTP/1.1\r\n"
                   f"Host: {host}:{port}\r\n"
                   f"User-Agent: NTRIP {APP_NAME}/{APP_VERSION}\r\n"
                   f"Authorization: Basic {auth}\r\n"
                   f"Ntrip-Version: Ntrip/2.0\r\n"
                   f"Connection: close\r\n"
                   f"\r\n")
            sock.sendall(req.encode())
            head, rest = self._read_header(sock)
            self._check_response(head)
        except BaseException:
            with contextlib.suppress(OSError):
                sock.close()
            raise
        return sock, rest

    def _read_header(self, sock):
        deadline = time.time() + HEADER_TIMEOUT
        buf = b""
        while b"\r\n\r\n" not in buf:
            if self._stop.is_set():
                raise RuntimeError("cancelled")
            if time.time() > deadline:
                raise RuntimeError("timed out waiting for the response header "
                                   "(the caster is not responding)")
            try:
                chunk = sock.recv(256)
            except socket.timeout:
                continue
            if not chunk:
                raise RuntimeError("connection closed before the response header arrived "
                                   "(bad credentials or unknown mountpoint)")
            buf += chunk
            if len(buf) > 8192:
                raise RuntimeError("response header is unreasonably long - "
                                   "this may not be an NTRIP service")
        head, rest = buf.split(b"\r\n\r\n", 1)
        return head, rest

    def _check_response(self, head):
        """On failure the server returns a plain-text error line (e.g. "ERROR - ..."),
        not an HTTP status code."""
        first = head.split(b"\r\n", 1)[0].decode("ascii", "ignore").strip()
        log.add("info", f"[ntrip] response: {first}")
        low = first.lower()
        if low.startswith("sourcetable"):
            raise RuntimeError("this is the caster's source table, not a data stream - "
                               "use a concrete mountpoint (e.g. NEAR-SPARTN)")
        if low.startswith("error"):
            detail = first[5:].strip(" -") or "unknown error"
            d = detail.lower()
            if "password" in d or "unauthor" in d or "auth" in d:
                raise RuntimeError(f"wrong username or password (caster: {detail})")
            if "mountpoint" in d or "not found" in d:
                raise RuntimeError(f"mountpoint does not exist (caster: {detail})")
            raise RuntimeError(f"caster refused the connection: {detail}")
        if "200" not in first:
            raise RuntimeError(f"NTRIP handshake failed: {first}")

    def _bootstrap_gga(self, cfg):
        return make_gga(cfg["bootstrap_lat"], cfg["bootstrap_lon"], 0.0)

    def _send_gga(self, sock, cfg):
        line, source = self._gnss.best_gga(self._bootstrap_gga(cfg))
        try:
            sock.sendall((line + "\r\n").encode())
        except socket.timeout:
            return False        # transient congestion; retry next cycle, not fatal
        except OSError:
            if self._stop.is_set():
                return False
            raise
        self._status.set(gga_source=source, last_gga_sent=time.time())
        return True

    def _push(self, payload):
        self._status.bump("bytes_total", len(payload))
        self._status.set(last_byte=time.time())
        for i in range(0, len(payload), SPARTN_CHUNK):
            try:
                with _BRIDGE_LOCK:
                    bridge.call("push_bytes", payload[i:i + SPARTN_CHUNK].hex())
            except Exception as e:  # noqa: BLE001
                # a bridge hiccup must not tear down a healthy NTRIP connection
                self._status.bump("push_errors")
                log.add("error", f"[bridge] push_bytes failed: {e}")
                return


# ============================== bridge callbacks ==============================

bridge = Bridge()
WORKER = None
STORE = None
GNSS = GnssState()
STATUS = Status()

_gga_log = {"quality": None, "t": 0.0}


def _decode_notify(data):
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, (bytes, bytearray)):
        return data.decode("ascii", "ignore").strip()
    if isinstance(data, list):
        return bytes(data).decode("ascii", "ignore").strip()
    return None


def on_gnss(data):
    """NMEA reported by the MCU. Must not raise - an exception here can destabilise
    the bridge."""
    try:
        raw = _decode_notify(data)
        if not raw:
            return
        g = parse_gga(raw)
        if g is None:
            GNSS.parse_failures += 1
            return
        GNSS.submit(g, raw)

        # Rate-limited logging: always on a quality change, otherwise every 5 s,
        # so the log does not get flooded
        now = time.time()
        if g["quality"] != _gga_log["quality"] or now - _gga_log["t"] >= 5.0:
            _gga_log["quality"] = g["quality"]
            _gga_log["t"] = now
            log.add("info", f"[GGA] quality={g['quality']} sv={g['numSV']} "
                            f"lat={g['lat']:.7f} lon={g['lon']:.7f} "
                            f"alt={g['alt']:.1f}m hdop={g['hdop']}")
    except Exception as e:  # noqa: BLE001
        GNSS.callback_errors += 1
        log.add("error", f"[gnss] callback error: {e}")


# ============================== Web layer ==============================


def build_status():
    n = STATUS.snapshot()
    g = GNSS.snapshot()
    cfg = STORE.get_public()
    return {
        "version": APP_VERSION,
        "uptime": round(time.time() - START_TIME, 1),
        "ntrip": {
            "state": n["ntrip_state"],
            "enabled": WORKER.is_enabled(),
            "epoch": n["epoch"],
            "connected_for": n["connected_for"],
            "last_error": n["last_error"],
            "host": n["host"], "port": n["port"], "mountpoint": n["mountpoint"],
            "bytes_total": n["bytes_total"],
            "bytes_per_sec": n["bytes_per_sec"],
            "last_byte_age": n["last_byte_age"],
            "push_errors": n["push_errors"],
            "reconnects": n["reconnects"],
            "gga_sent": n["gga_sent"],
            "last_gga_sent_age": n["last_gga_sent_age"],
            "gga_source": n["gga_source"],
            "awaiting_data": n["awaiting_data"],
        },
        "gnss": g,
        "config": {
            "ok": STORE.load_error is None,
            "writable": STORE.writable,
            "path": STORE.path,
            "path_reason": STORE.path_reason,
            "load_error": STORE.load_error,
            "complete": cfg["config_complete"],
        },
        "signup_url": SIGNUP_URL,
        "webui_available": WEBUI_AVAILABLE,
    }


def _as_dict(data):
    """Normalise the POST body into a dict (the brick passes a dict; this is a safety net)."""
    if isinstance(data, dict):
        return data
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "ignore")
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return None


def register_web(ui):
    # WARNING: do not change these signatures casually. expose_api registers the function
    # directly with FastAPI, and FastAPI treats a parameter as a required QUERY parameter
    # when it has a scalar annotation (int/str/bool/...) or NO annotation at all. So:
    #   - a POST body must be declared as `data: dict`. Dropping the `: dict` turns it
    #     into a required query parameter and the endpoint returns 422
    #     ({"detail":[{"type":"missing","loc":["query","data"]...}]});
    #   - do not add *args/**kwargs "for compatibility" - they become required query
    #     parameters too and break every endpoint.
    def api_status():
        return build_status()

    def api_config_get():
        return STORE.get_public()

    def api_config_post(data: dict):
        payload = _as_dict(data)
        if payload is None:
            return {"ok": False,
                    "error": f"request body is not a JSON object "
                             f"(server received {type(data).__name__})"}
        try:
            public, warnings = STORE.save(payload)
        except ConfigError as e:
            return {"ok": False, "error": str(e)}
        # Applied asynchronously: only bump the epoch and return. Waiting for the
        # reconnect here would stall the whole web server - including the status poll
        # the user is currently watching - for as long as a slow stop takes.
        epoch = WORKER.set_config(STORE.get())
        log.add("info", "[web] config saved and applied")
        return {"ok": True, "applied": True, "epoch": epoch,
                "warnings": warnings, "config": public}

    def api_worker_post(data: dict):
        action = (_as_dict(data) or {}).get("action")
        if action not in ("start", "stop"):
            return {"ok": False, "error": "action must be start or stop"}
        try:
            epoch = WORKER.set_enabled(action == "start")
        except ConfigError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "enabled": WORKER.is_enabled(), "epoch": epoch}

    def api_events():
        # Returns the most recent 100 lines for troubleshooting
        return {"lines": log.snapshot(limit=100), "next": log.next_seq()}

    ui.expose_api("GET", "/api/status", api_status)
    ui.expose_api("GET", "/api/config", api_config_get)
    ui.expose_api("POST", "/api/config", api_config_post)
    ui.expose_api("POST", "/api/worker", api_worker_post)
    ui.expose_api("GET", "/api/events", api_events)
    log.add("info", "[web] configuration UI ready (port 7000)")


# ============================== Entry point ==============================


def _on_sigterm(signum, frame):
    log.add("info", "[app] received SIGTERM, shutting down ...")
    if WORKER:
        WORKER.shutdown()
    raise SystemExit(0)


def main():
    global WORKER, STORE

    log.add("info", f"{APP_NAME} v{APP_VERSION} starting")
    if not WEBUI_AVAILABLE:
        log.add("warn", f"WebUI brick unavailable ({WEBUI_IMPORT_ERROR}); "
                        f"running without the configuration UI")
    # A wrong system clock makes the caster reject the GGA timestamps we report
    if time.gmtime().tm_year < 2024:
        log.add("warn", "[app] system clock looks wrong, check NTP")

    path, reason = resolve_config_path()
    STORE = ConfigStore(path, reason)
    log.add("info", f"config path: {path} ({reason})")
    cfg = STORE.load()
    if not cfg["username"] or not cfg["password"]:
        log.add("warn", "NTRIP credentials are not configured yet - fill them in "
                        "the configuration UI")

    bridge.provide("gnss", on_gnss)

    WORKER = NtripWorker(GNSS, STATUS)
    WORKER.start()
    if config_complete(cfg):
        WORKER.set_config(cfg)
    else:
        STATUS.set(ntrip_state="stopped")

    if WEBUI_AVAILABLE:
        try:
            register_web(WebUI())
        except Exception as e:  # noqa: BLE001 - a broken web layer must not affect RTK
            log.add("error", f"[web] failed to start the configuration UI: {e}")

    atexit.register(WORKER.shutdown)
    with contextlib.suppress(Exception):
        signal.signal(signal.SIGTERM, _on_sigterm)
    return cfg


if __name__ == "__main__":
    main()
    App.run()
