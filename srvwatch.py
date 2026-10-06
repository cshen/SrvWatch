#!/usr/bin/env python3
"""
SrvWatch — a tiny, self-contained server-health monitor.

Runs an HTTP service on port 9001 that reports the health of this machine:
  * CPU / GPU temperature and utilization, fans, power, memory (via macmon)
  * whether the MediaSrv media server is running and responding
  * whether SSH (Remote Login) is up
  * disk usage, load average, uptime, top processes, network addresses

Everything is standard-library only (no pip dependencies), so the monitor
keeps working even if the machine's Python environments break.

Endpoints:
    GET /            human-friendly HTML dashboard (auto-refreshes)
    GET /api/status  full JSON snapshot
    GET /health      liveness probe -> {"status": "ok"}
"""

import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

# --------------------------------------------------------------------------
# Configuration
#
# Resolution order (later wins):
#   1. built-in defaults (below)
#   2. config.toml  (next to this script, or ~/.config/SrvWatch/config.toml,
#                    or the path in $SRVWATCH_CONFIG)
#   3. environment variables (SRVWATCH_*)
# --------------------------------------------------------------------------


def _parse_toml(text):
    """Parse the small TOML subset this project uses: [sections], key = value,
    '#' comments, and string / int / float / bool values."""
    data = {}
    section = data

    def _value(s):
        # Strip a trailing '#' comment that isn't inside quotes.
        quote = None
        for i, ch in enumerate(s):
            if ch in ('"', "'"):
                if quote is None:
                    quote = ch
                elif quote == ch:
                    quote = None
            elif ch == "#" and quote is None and (i == 0 or s[i - 1] in " \t"):
                s = s[:i]
                break
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            return s[1:-1]
        low = s.lower()
        if low in ("true", "false"):
            return low == "true"
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return s

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if line.endswith("]"):
                section = data.setdefault(line[1:-1].strip(), {})
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            section[key.strip()] = _value(val)
    return data


def _find_config_path():
    env = os.environ.get("SRVWATCH_CONFIG")
    if env:
        p = os.path.expanduser(env)
        if os.path.isfile(p):
            return p
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, "config.toml")
    if os.path.isfile(local):
        return local
    user = os.path.expanduser("~/.config/SrvWatch/config.toml")
    if os.path.isfile(user):
        return user
    return None


def _env_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config():
    cfg = {
        "host": "0.0.0.0",
        "port": 9001,
        "refresh": 5,          # seconds between collections
        "theme": "dark",       # "dark" | "light"
        "macmon": shutil.which("macmon") or "/opt/homebrew/bin/macmon",
        "mediasrv_port": 9000,
        "mediasrv_url": None,  # computed below from the port
        "mediasrv_pattern": "mediasrv",
        "ssh_port": 22,
        "weather_command": "~/bin/aqi.sh -j",
        "weather_interval": 3600,  # seconds between weather refreshes
        "weather_timeout": 30,     # seconds to allow the script to run
        "forecast_days": 2,        # future days shown in the forecast table
        "temp_warn": 70.0,     # deg C
        "temp_crit": 85.0,     # deg C
        "disk_warn": 80.0,     # percent used
        "disk_crit": 90.0,     # percent used
        # fail2ban (brute-force protection). Liveness via pgrep needs no root;
        # jail/ban detail comes from an optional status file (see deploy/).
        "fail2ban_enabled": True,
        "fail2ban_pattern": "fail2ban",
        "fail2ban_status_file": "~/Library/Logs/SrvWatch/fail2ban-status.json",
        # Reverse SSH tunnel kept alive by autossh (see ~/bin/ssh_reverse.sh).
        "tunnel_enabled": True,
        "tunnel_pattern": "autossh",
        "tunnel_remote_port": None,   # require this -R forward (e.g. 2292); None = any
        # External drive that should stay available, plus its SMART health.
        "drive_label": None,
        "drive_mount": None,          # expected mount point, e.g. /Volumes/MyDrive
        "drive_device": None,         # diskutil selector used even when unmounted, e.g. disk6s1
        "drive_required": False,      # raise an issue when the drive is absent
    }

    path = _find_config_path()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                d = _parse_toml(f.read())
        except Exception as e:
            print(f"warning: could not read {path}: {e}", flush=True)
            d = {}
        mapping = {
            "host": d.get("server", {}).get("host"),
            "port": d.get("server", {}).get("port"),
            "refresh": d.get("monitor", {}).get("refresh"),
            "theme": d.get("theme", {}).get("theme"),
            "macmon": d.get("macmon", {}).get("path"),
            "mediasrv_port": d.get("mediasrv", {}).get("port"),
            "mediasrv_url": d.get("mediasrv", {}).get("url"),
            "mediasrv_pattern": d.get("mediasrv", {}).get("pattern"),
            "ssh_port": d.get("ssh", {}).get("port"),
            "weather_command": d.get("weather", {}).get("command"),
            "weather_interval": d.get("weather", {}).get("interval"),
            "weather_timeout": d.get("weather", {}).get("timeout"),
            "forecast_days": d.get("weather", {}).get("forecast_days"),
            "temp_warn": d.get("thresholds", {}).get("temp_warn"),
            "temp_crit": d.get("thresholds", {}).get("temp_crit"),
            "disk_warn": d.get("thresholds", {}).get("disk_warn"),
            "disk_crit": d.get("thresholds", {}).get("disk_crit"),
            "fail2ban_enabled": d.get("fail2ban", {}).get("enabled"),
            "fail2ban_pattern": d.get("fail2ban", {}).get("pattern"),
            "fail2ban_status_file": d.get("fail2ban", {}).get("status_file"),
            "tunnel_enabled": d.get("tunnel", {}).get("enabled"),
            "tunnel_pattern": d.get("tunnel", {}).get("pattern"),
            "tunnel_remote_port": d.get("tunnel", {}).get("remote_port"),
            "drive_label": d.get("drive", {}).get("label"),
            "drive_mount": d.get("drive", {}).get("mount"),
            "drive_device": d.get("drive", {}).get("device"),
            "drive_required": d.get("drive", {}).get("required"),
        }
        for key, value in mapping.items():
            if value is not None:
                cfg[key] = value

        if isinstance(d.get("checks"), dict):
            cfg["checks"] = d["checks"]

    env_map = {
        "SRVWATCH_HOST": ("host", str),
        "SRVWATCH_PORT": ("port", int),
        "SRVWATCH_REFRESH": ("refresh", int),
        "SRVWATCH_THEME": ("theme", str),
        "SRVWATCH_MACMON": ("macmon", str),
        "SRVWATCH_MEDIASRV_PORT": ("mediasrv_port", int),
        "SRVWATCH_MEDIASRV_URL": ("mediasrv_url", str),
        "SRVWATCH_MEDIASRV_PATTERN": ("mediasrv_pattern", str),
        "SRVWATCH_SSH_PORT": ("ssh_port", int),
        "SRVWATCH_WEATHER_COMMAND": ("weather_command", str),
        "SRVWATCH_WEATHER_INTERVAL": ("weather_interval", int),
        "SRVWATCH_WEATHER_TIMEOUT": ("weather_timeout", int),
        "SRVWATCH_WEATHER_FORECAST_DAYS": ("forecast_days", int),
        "SRVWATCH_FAIL2BAN_ENABLED": ("fail2ban_enabled", _env_bool),
        "SRVWATCH_FAIL2BAN_PATTERN": ("fail2ban_pattern", str),
        "SRVWATCH_FAIL2BAN_STATUS_FILE": ("fail2ban_status_file", str),
        "SRVWATCH_TUNNEL_ENABLED": ("tunnel_enabled", _env_bool),
        "SRVWATCH_TUNNEL_PATTERN": ("tunnel_pattern", str),
        "SRVWATCH_TUNNEL_REMOTE_PORT": ("tunnel_remote_port", int),
        "SRVWATCH_DRIVE_LABEL": ("drive_label", str),
        "SRVWATCH_DRIVE_MOUNT": ("drive_mount", str),
        "SRVWATCH_DRIVE_DEVICE": ("drive_device", str),
        "SRVWATCH_DRIVE_REQUIRED": ("drive_required", _env_bool),
    }
    for var, (key, cast) in env_map.items():
        if var in os.environ:
            try:
                cfg[key] = cast(os.environ[var])
            except ValueError:
                pass

    if not cfg["mediasrv_url"]:
        cfg["mediasrv_url"] = f"http://127.0.0.1:{cfg['mediasrv_port']}/"
    if cfg["theme"] not in ("dark", "light"):
        cfg["theme"] = "dark"
    return cfg


CONFIG = load_config()

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def run_cmd(argv, timeout=5):
    """Run a command and return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # FileNotFound, TimeoutExpired, ...
        return -1, "", str(e)


def check_port(host, port, timeout=2.0):
    """Return (open: bool, error: str|None) for a TCP connect test."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except Exception as e:
        return False, str(e)


def fmt_bytes(n):
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.1f} {unit}"
        n /= 1024.0


def fmt_uptime(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


# --------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------


def get_local_ips():
    ips = set()
    for iface in ("en0", "en1", "en2", "bridge0"):
        rc, out, _ = run_cmd(["ipconfig", "getifaddr", iface], timeout=2)
        if rc == 0 and out.strip():
            ips.add(out.strip())
    # Fallback: route-based guess (needs a default route; may yield 127.0.0.1)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips)


def collect_system():
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "os_version": platform.mac_ver()[0] or None,
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "python": platform.python_version(),
    }


def collect_uptime():
    rc, out, _ = run_cmd(["sysctl", "-n", "kern.boottime"], timeout=2)
    m = re.search(r"sec\s*=\s*(\d+)", out)
    boot = int(m.group(1)) if m else 0
    if not boot:
        return None
    return max(0, int(time.time()) - boot)


def collect_macmon():
    """One macmon JSON snapshot. Returns a flattened dict."""
    rc, out, err = run_cmd([CONFIG["macmon"], "pipe", "-s", "1", "-i", "1000"], timeout=10)
    if rc != 0 or not out.strip():
        return {"available": False, "error": err or "macmon produced no output"}
    try:
        line = out.strip().splitlines()[-1]
        d = json.loads(line)
    except Exception as e:
        return {"available": False, "error": f"bad JSON: {e}"}

    temp = d.get("temp", {})
    mem = d.get("memory", {})
    gpu_usage = d.get("gpu_usage") or [None, None]
    return {
        "available": True,
        "cpu_temp_avg": temp.get("cpu_temp_avg"),
        "gpu_temp_avg": temp.get("gpu_temp_avg"),
        "cpu_usage_pct": d.get("cpu_usage_pct"),
        "gpu_usage_pct": gpu_usage[1] if len(gpu_usage) > 1 else None,
        "ecpu_usage": d.get("ecpu_usage"),
        "pcpu_usage": d.get("pcpu_usage"),
        "fans": d.get("fans", []),
        "sys_power": d.get("sys_power"),
        "all_power": d.get("all_power"),
        "memory": mem,
        "sample_timestamp": d.get("timestamp"),
    }


def check_mediasrv():
    result = {
        "label": "MediaSrv",
        "url": CONFIG["mediasrv_url"],
        "status": "down",
        "pid": None,
        "port_open": False,
        "http_status": None,
        "http_latency_ms": None,
    }
    # Process
    rc, out, _ = run_cmd(["pgrep", "-f", CONFIG["mediasrv_pattern"]], timeout=3)
    pids = [p for p in out.split() if p.strip() and p.strip() != str(os.getpid())]
    if pids:
        try:
            result["pid"] = int(pids[0])
        except ValueError:
            pass
    # TCP port
    open_, _err = check_port("127.0.0.1", CONFIG["mediasrv_port"], timeout=2)
    result["port_open"] = open_
    # HTTP round trip
    try:
        t0 = time.time()
        req = urllib.request.Request(CONFIG["mediasrv_url"], method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            result["http_status"] = resp.status
            result["http_latency_ms"] = round((time.time() - t0) * 1000, 1)
    except Exception:
        result["http_status"] = None
        result["http_latency_ms"] = None
    # Derive status
    if result["http_status"] == 200:
        result["status"] = "ok"
    elif result["port_open"] or result["pid"]:
        result["status"] = "degraded"
    else:
        result["status"] = "down"
    return result


def check_ssh():
    open_, err = check_port("127.0.0.1", CONFIG["ssh_port"], timeout=2)
    rc, out, _ = run_cmd(["pgrep", "-f", "sshd"], timeout=3)
    sshd_procs = len([p for p in out.split() if p.strip()])
    return {
        "label": "SSH",
        "status": "ok" if open_ else "down",
        "port": CONFIG["ssh_port"],
        "port_open": open_,
        "sshd_processes": sshd_procs,
        "error": err,
    }


def check_fail2ban():
    """fail2ban liveness (process, no root needed) plus jail/ban detail from an
    optional status file written by a root helper (see deploy/fail2ban-status.py)."""
    result = {
        "label": "fail2ban",
        "status": "down",
        "pids": [],
        "jails": None,
        "banned": None,
        "updated": None,
        "stale": False,
        "error": None,
    }
    if not CONFIG.get("fail2ban_enabled", True):
        result["status"] = "disabled"
        return result

    rc, out, _ = run_cmd(["pgrep", "-fl", str(CONFIG["fail2ban_pattern"])], timeout=3)
    for line in out.splitlines():
        pid, _, _cmd = line.partition(" ")
        pid = pid.strip()
        if pid.isdigit() and int(pid) != os.getpid():
            result["pids"].append(pid)

    status = _load_fail2ban_status()
    if status:
        result["jails"] = status.get("jails")
        result["banned"] = status.get("banned")
        result["updated"] = status.get("updated")
        if status.get("error"):
            result["error"] = status["error"]
        updated = status.get("updated")
        if updated:
            try:
                age = time.time() - datetime.fromisoformat(updated).timestamp()
                result["stale"] = age > 600
            except Exception:
                pass

    result["status"] = "ok" if result["pids"] else "down"
    return result


def check_tunnel():
    """Presence of the reverse-SSH tunnel kept alive by autossh. When a remote
    port is configured, an autossh process without that -R forward counts as
    degraded rather than down."""
    result = {
        "label": "SSH tunnel",
        "status": "down",
        "pid": None,
        "command": None,
        "remote_port": CONFIG.get("tunnel_remote_port"),
    }
    if not CONFIG.get("tunnel_enabled", True):
        result["status"] = "disabled"
        return result

    rc, out, _ = run_cmd(["pgrep", "-fl", str(CONFIG["tunnel_pattern"])], timeout=3)
    candidates = []
    for line in out.splitlines():
        pid, _, cmd = line.partition(" ")
        pid = pid.strip()
        if pid.isdigit() and int(pid) != os.getpid():
            candidates.append((pid, cmd.strip()))

    port = CONFIG.get("tunnel_remote_port")
    chosen = None
    for pid, cmd in candidates:
        if port:
            if re.search(rf"(^|\s)-R\s*{int(port)}:", cmd):
                chosen = (pid, cmd)
                break
        else:
            chosen = (pid, cmd)
            break

    if chosen:
        result["pid"] = chosen[0]
        result["command"] = chosen[1][:200]
        result["status"] = "ok"
    elif candidates and port:
        # autossh is alive but not forwarding the expected port.
        result["pid"] = candidates[0][0]
        result["command"] = candidates[0][1][:200]
        result["status"] = "degraded"
    return result


def _load_fail2ban_status():
    path = os.path.expanduser(str(CONFIG.get("fail2ban_status_file") or ""))
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _diskutil_info(selector, timeout=6):
    """`diskutil info -plist <selector>` as a dict, or None if unavailable."""
    if not selector:
        return None
    rc, out, _ = run_cmd(["diskutil", "info", "-plist", str(selector)], timeout=timeout)
    if rc != 0 or not out.strip():
        return None
    try:
        return plistlib.loads(out.encode("utf-8"))
    except Exception:
        return None


def check_smart(device):
    """SMART health for a whole-disk device (e.g. 'disk0'/'disk3') via diskutil.
    Needs no root. Returns None when the device can't be queried."""
    info = _diskutil_info(device)
    if not info:
        return None
    return {
        "device": device,
        "status": info.get("SMARTStatus") or "Unknown",
        "solid_state": info.get("SolidState"),
        "media": info.get("MediaName") or None,
    }


def inspect_drive(selector, mount=None, label=None, required=False):
    """Inspect one drive: presence, mount state, capacity, and SMART health."""
    info = _diskutil_info(selector)
    entry = {
        "label": label or "drive",
        "selector": selector,
        "mount": mount,
        "required": bool(required),
        "found": info is not None,
        "mounted": False,
        "volume_name": None,
        "device": None,
        "whole_disk": None,
        "internal": None,
        "smart": None,
        "pct": None,
        "used": None,
        "total": None,
        "free": None,
    }
    if not info:
        return entry

    whole = info.get("DeviceIdentifier") if info.get("WholeDisk") else info.get("ParentWholeDisk")
    vname = info.get("VolumeName") or info.get("MediaName")
    entry["volume_name"] = vname or None
    entry["device"] = info.get("DeviceIdentifier")
    entry["whole_disk"] = whole
    entry["internal"] = info.get("Internal")
    if not label and vname:
        entry["label"] = vname

    mp = mount or info.get("MountPoint") or None
    entry["mounted"] = bool(mp) and os.path.ismount(mp)
    if entry["mounted"]:
        entry["mount"] = mp
        try:
            u = shutil.disk_usage(mp)
            entry["total"] = u.total
            entry["used"] = u.used
            entry["free"] = u.free
            entry["pct"] = round(u.used / u.total * 100, 1) if u.total else 0.0
        except Exception:
            pass

    entry["smart"] = check_smart(whole)
    return entry


def collect_drives():
    """The boot volume + the configured external drive, each with SMART."""
    drives = [inspect_drive("/", mount="/", label="/")]
    if CONFIG.get("drive_mount") or CONFIG.get("drive_device"):
        drives.append(
            inspect_drive(
                CONFIG.get("drive_device") or CONFIG.get("drive_mount"),
                mount=CONFIG.get("drive_mount"),
                label=CONFIG.get("drive_label"),
                required=CONFIG.get("drive_required", False),
            )
        )
    return drives


def collect_disk():
    volumes = []

    def volume_info(path, label):
        try:
            u = shutil.disk_usage(path)
            return {
                "label": label,
                "path": path,
                "total": u.total,
                "used": u.used,
                "free": u.free,
                "pct": round(u.used / u.total * 100, 1) if u.total else 0.0,
            }
        except Exception as e:
            return {"label": label, "path": path, "error": str(e)}

    volumes.append(volume_info("/", "/"))
    if os.path.isdir("/Volumes"):
        for name in sorted(os.listdir("/Volumes")):
            if name == "Recovery":
                continue  # macOS system recovery volume, not user storage
            p = os.path.join("/Volumes", name)
            if os.path.ismount(p):
                volumes.append(volume_info(p, name))
    return volumes


def collect_top_processes(limit=6):
    rc, out, _ = run_cmd(
        ["ps", "-A", "-o", "pid=,%cpu=,%mem=,comm=", "-r"], timeout=3
    )
    rows = []
    for line in out.splitlines():
        if len(rows) >= limit:
            break
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append(
                {"pid": parts[0], "cpu": parts[1], "mem": parts[2], "name": parts[3]}
            )
    return rows


# --------------------------------------------------------------------------
# Weather / air quality (from ~/bin/aqi.sh)
# --------------------------------------------------------------------------
_weather_lock = threading.Lock()
_weather = {"available": False, "loading": True}


def _http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "SrvWatch/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_forecast(lat, lon, days):
    """Daily forecast for the next `days` days (excluding today) from Open-Meteo:
    temperature + WMO condition from the forecast API, and daily US AQI (the
    day's peak of the hourly values) from the air-quality API."""
    wx_q = urlencode({
        "latitude": lat,
        "longitude": lon,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": days + 1,
    })
    aq_q = urlencode({
        "latitude": lat,
        "longitude": lon,
        "hourly": "us_aqi",
        "timezone": "auto",
        "forecast_days": days + 1,
    })
    wx = _http_json("https://api.open-meteo.com/v1/forecast?" + wx_q)
    aq = _http_json("https://air-quality-api.open-meteo.com/v1/air-quality?" + aq_q)

    wdaily = wx.get("daily") or {}
    times = wdaily.get("time") or []
    codes = wdaily.get("weather_code") or []
    tmaxs = wdaily.get("temperature_2m_max") or []
    tmins = wdaily.get("temperature_2m_min") or []

    # Daily peak US AQI, aggregated from the hourly series by date.
    hourly = aq.get("hourly") or {}
    htimes = hourly.get("time") or []
    hvals = hourly.get("us_aqi") or []
    aqi_by_date = {}
    for i, ts in enumerate(htimes):
        v = hvals[i] if i < len(hvals) else None
        if v is None:
            continue
        d = ts.split("T")[0]
        aqi_by_date[d] = max(aqi_by_date.get(d, v), v)

    days_out = []
    for i, d in enumerate(times):
        days_out.append({
            "date": d,
            "code": codes[i] if i < len(codes) else None,
            "tmax": tmaxs[i] if i < len(tmaxs) else None,
            "tmin": tmins[i] if i < len(tmins) else None,
            "aqi": round(aqi_by_date[d]) if d in aqi_by_date else None,
        })
    # The first entry is today; return only the following `days` days.
    return days_out[1:1 + days]


def fetch_weather():
    cmd = CONFIG.get("weather_command", "").strip()
    if not cmd:
        return {"available": False, "error": "weather disabled (empty command)"}
    argv = shlex.split(cmd)
    if argv:
        argv[0] = os.path.expanduser(argv[0])
    rc, out, err = run_cmd(argv, timeout=CONFIG["weather_timeout"])
    if rc != 0 or not out.strip():
        return {
            "available": False,
            "error": (err or f"exit code {rc}").strip() or "no output",
        }
    try:
        parsed = json.loads(out)
    except Exception as e:
        return {"available": False, "error": f"bad JSON: {e}"}
    parsed["available"] = True
    parsed["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Attach a short-range daily forecast, using the coordinates the weather
    # command already resolved (keeps ~/bin/aqi.sh untouched).
    days = CONFIG.get("forecast_days", 0) or 0
    loc = parsed.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if days > 0 and lat is not None and lon is not None:
        try:
            parsed["forecast"] = fetch_forecast(lat, lon, days)
        except Exception as e:
            parsed["forecast_error"] = str(e)
    return parsed


def weather_loop():
    global _weather
    while True:
        data = fetch_weather()
        with _weather_lock:
            _weather = data
        time.sleep(CONFIG["weather_interval"])


def get_weather():
    with _weather_lock:
        return dict(_weather)


# --------------------------------------------------------------------------
# Security (SSH sessions, fail2ban, firewall, FileVault, failed logins)
# --------------------------------------------------------------------------
_ssh_log_lock = threading.Lock()
_ssh_log_cache = {"ts": 0.0, "failed": None}


def collect_ssh_failures():
    """Count failed/invalid SSH auth lines in the last hour from the unified
    log. Cached for 5 minutes because `log show` is relatively slow."""
    now = time.time()
    with _ssh_log_lock:
        if _ssh_log_cache["failed"] is not None and (now - _ssh_log_cache["ts"]) < 300:
            return {"window": "1h", "failed": _ssh_log_cache["failed"]}
    rc, out, _ = run_cmd(
        ["log", "show", "--last", "1h", "--predicate",
         'process == "sshd"', "--style", "compact"],
        timeout=20,
    )
    failed = 0
    patterns = ("failed password", "invalid user", "authentication failure",
                "failed publickey", "too many authentication")
    for line in out.splitlines():
        low = line.lower()
        if any(p in low for p in patterns):
            failed += 1
    with _ssh_log_lock:
        _ssh_log_cache["ts"] = now
        _ssh_log_cache["failed"] = failed
    return {"window": "1h", "failed": failed}


def collect_security():
    result = {}

    # Active interactive sessions
    rc, out, _ = run_cmd(["who"], timeout=3)
    sessions = [ln for ln in out.splitlines() if ln.strip()]
    sources = []
    for s in sessions:
        m = re.search(r"\(([^)]+)\)", s)
        if m and m.group(1) not in sources:
            sources.append(m.group(1))
    result["active_sessions"] = len(sessions)
    result["sources"] = sources

    # Firewall
    rc, out, _ = run_cmd(
        ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"], timeout=3
    )
    low = out.lower()
    result["firewall"] = (
        "enabled" if ("enabled" in low and "disabled" not in low)
        else ("disabled" if "disabled" in low else "unknown")
    )

    # FileVault
    rc, out, _ = run_cmd(["fdesetup", "status"], timeout=3)
    result["filevault"] = (
        "on" if "FileVault is On" in out
        else ("off" if "FileVault is Off" in out else "unknown")
    )

    result["ssh_failures"] = collect_ssh_failures()
    return result


# --------------------------------------------------------------------------
# Connectivity (DNS, internet, default route)
# --------------------------------------------------------------------------
def collect_connectivity():
    result = {}

    t0 = time.time()
    try:
        ip = socket.gethostbyname("api.open-meteo.com")
        result["dns"] = {"status": "ok", "host": "api.open-meteo.com", "ip": ip,
                         "ms": round((time.time() - t0) * 1000)}
    except Exception as e:
        result["dns"] = {"status": "down", "host": "api.open-meteo.com",
                         "error": str(e), "ms": round((time.time() - t0) * 1000)}

    t0 = time.time()
    ok, err = check_port("1.1.1.1", 443, timeout=3)
    result["internet"] = {"status": "ok" if ok else "down", "target": "1.1.1.1:443",
                          "ms": round((time.time() - t0) * 1000), "error": err}

    rc, out, _ = run_cmd(["route", "-n", "get", "default"], timeout=3)
    m = re.search(r"interface:\s*(\S+)", out)
    iface = m.group(1) if m else None
    m = re.search(r"gateway:\s*(\S+)", out)
    gw = m.group(1) if m else None
    result["route"] = {
        "interface": iface,
        "gateway": gw,
        "tunneled": bool(iface and iface.startswith(("utun", "ppp", "tun", "tap"))),
    }
    return result


# --------------------------------------------------------------------------
# Backup & disk health
# --------------------------------------------------------------------------
def collect_disk_io():
    rc, out, _ = run_cmd(["iostat", "-d", "-c", "2", "-w", "1"], timeout=8)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    names = lines[0].split()
    data = lines[-1].split()
    disks = {}
    for i, name in enumerate(names):
        try:
            disks[name] = {
                "tps": float(data[i * 3 + 1]),
                "mb_s": float(data[i * 3 + 2]),
            }
        except (IndexError, ValueError):
            continue
    return {"disks": disks}


def collect_backup_health():
    result = {}

    rc, out, err = run_cmd(["tmutil", "latestbackup"], timeout=8)
    text = (out or "").strip() or (err or "").strip()
    latest = None
    error = None
    if text and re.search(r"\d{4}-\d{2}-\d{2}-\d{6}", text):
        latest = text.splitlines()[0].strip()
    elif text:
        error = text.splitlines()[0][:140]

    age_days = None
    if latest:
        m = re.search(r"(\d{4}-\d{2}-\d{2})-(\d{6})", latest)
        if m:
            try:
                dt = datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S")
                age_days = round((time.time() - dt.timestamp()) / 86400, 1)
            except Exception:
                pass

    rc, sout, _ = run_cmd(["tmutil", "status"], timeout=5)
    running = bool(re.search(r'"?Running"?\s*=\s*1', sout))

    result["time_machine"] = {
        "latest": latest, "age_days": age_days, "running": running, "error": error,
    }

    result["io"] = collect_disk_io()
    return result


# --------------------------------------------------------------------------
# Listening TCP ports / exposed services
# --------------------------------------------------------------------------
def collect_listening_ports():
    rc, out, _ = run_cmd(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=6)
    seen = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        cmd = parts[0]
        name = parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]
        if ":" not in name:
            continue
        addr, _, port = name.rpartition(":")
        try:
            pnum = int(port)
        except ValueError:
            continue
        key = (cmd, pnum)
        if key in seen:
            seen[key]["count"] += 1
            continue
        seen[key] = {
            "command": cmd,
            "address": addr,
            "port": pnum,
            "exposed": addr in ("*", "0.0.0.0", "::", "[::]"),
            "count": 1,
        }
    return sorted(seen.values(), key=lambda r: r["port"])


# --------------------------------------------------------------------------
# Extra user-configured service checks ([checks] in config.toml)
# --------------------------------------------------------------------------
def check_extra(name, spec):
    kind, _, target = (spec or "").partition(":")
    kind = kind.strip().lower()
    target = target.strip()
    out = {"name": name, "spec": spec, "status": "unknown", "detail": ""}

    if kind == "process" and target:
        rc, o, _ = run_cmd(["pgrep", "-f", target], timeout=3)
        pids = [p for p in o.split() if p.strip() and p.strip() != str(os.getpid())]
        out["status"] = "ok" if pids else "down"
        out["detail"] = f"pid {pids[0]}" if pids else "not running"
    elif kind == "port" and target:
        try:
            portnum = int(target)
            ok, _err = check_port("127.0.0.1", portnum, timeout=2)
            out["status"] = "ok" if ok else "down"
            out["detail"] = f"tcp/{portnum} " + ("open" if ok else "closed")
        except ValueError:
            out["detail"] = f"bad port '{target}'"
    elif kind == "url" and target:
        try:
            t0 = time.time()
            with urllib.request.urlopen(urllib.request.Request(target, method="GET"), timeout=3) as resp:
                code = resp.status
            out["status"] = "ok" if code < 400 else "degraded"
            out["detail"] = f"HTTP {code} · {round((time.time() - t0) * 1000)}ms"
        except Exception as e:
            out["status"] = "down"
            out["detail"] = str(e)[:60]
    else:
        out["detail"] = "spec must be process:NAME | port:N | url:URL"
    return out


def collect_extra_checks():
    checks = CONFIG.get("checks") or {}
    return [check_extra(name, spec) for name, spec in checks.items()]


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------


def collect_all():
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "collected_at": time.time(),
        "system": collect_system(),
        "local_ips": get_local_ips(),
        "uptime_seconds": collect_uptime(),
        "loadavg": list(os.getloadavg()),
        "hardware": collect_macmon(),
        "mediasrv": check_mediasrv(),
        "ssh": check_ssh(),
        "fail2ban": check_fail2ban(),
        "tunnel": check_tunnel(),
        "drives": collect_drives(),
        "security": collect_security(),
        "connectivity": collect_connectivity(),
        "backup": collect_backup_health(),
        "ports": collect_listening_ports(),
        "checks": collect_extra_checks(),
        "weather": get_weather(),
        "disk": collect_disk(),
        "top_processes": collect_top_processes(),
    }

    issues = []
    if data["mediasrv"]["status"] != "ok":
        issues.append(f"mediasrv:{data['mediasrv']['status']}")
    if data["ssh"]["status"] != "ok":
        issues.append(f"ssh:{data['ssh']['status']}")
    if data["fail2ban"]["status"] == "down":
        issues.append("fail2ban:down")
    if data["tunnel"]["status"] in ("down", "degraded"):
        issues.append(f"tunnel:{data['tunnel']['status']}")

    for d in data["drives"]:
        smart = d.get("smart") or {}
        smart_status = (smart.get("status") or "").lower()
        if smart_status.startswith("fail"):
            issues.append(f"smart:{d['label']}:critical")
        if d.get("required") and not d.get("mounted"):
            issues.append(f"drive:{d['label']}:not-mounted")

    for c in data["checks"]:
        if c.get("status") != "ok":
            issues.append(f"check:{c['name']}:{c['status']}")
    conn = data["connectivity"]
    if conn["dns"]["status"] != "ok":
        issues.append("dns:down")
    if conn["internet"]["status"] != "ok":
        issues.append("internet:down")

    hw = data["hardware"]
    if not hw.get("available"):
        issues.append("macmon:unavailable")
    else:
        ct, gt = hw.get("cpu_temp_avg"), hw.get("gpu_temp_avg")
        if ct is not None and ct > CONFIG["temp_crit"]:
            issues.append("cpu_temp:critical")
        elif ct is not None and ct > CONFIG["temp_warn"]:
            issues.append("cpu_temp:warm")
        if gt is not None and gt > CONFIG["temp_crit"]:
            issues.append("gpu_temp:critical")
        elif gt is not None and gt > CONFIG["temp_warn"]:
            issues.append("gpu_temp:warm")

    for v in data["disk"]:
        if v.get("pct") is not None and v.get("label") == "/":
            if v["pct"] > CONFIG["disk_crit"]:
                issues.append(f"disk:{v['pct']}%")
            elif v["pct"] > CONFIG["disk_warn"]:
                issues.append(f"disk:{v['pct']}%")

    if not issues:
        data["status"] = "ok"
    elif any(
        "critical" in s or s.startswith("ssh:down") or s.startswith("mediasrv:down")
        for s in issues
    ):
        data["status"] = "critical"
    else:
        data["status"] = "degraded"

    data["issues"] = issues
    return data


# --------------------------------------------------------------------------
# Background collector thread
# --------------------------------------------------------------------------
_state_lock = threading.Lock()
_current = None


def collector_loop():
    global _current
    while True:
        try:
            snapshot = collect_all()
        except Exception as e:  # keep the loop alive no matter what
            snapshot = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "critical",
                "issues": [f"collector error: {e}"],
            }
        with _state_lock:
            _current = snapshot
        time.sleep(CONFIG["refresh"])


def get_snapshot():
    with _state_lock:
        return _current


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: dark;
  --bg: oklch(0.18 0.018 174);
  --bg-deep: oklch(0.145 0.016 174);
  --panel: oklch(0.225 0.018 174);
  --panel-raised: oklch(0.255 0.02 174);
  --line: oklch(0.34 0.018 174);
  --line-strong: oklch(0.43 0.025 174);
  --fg: oklch(0.92 0.014 88);
  --fg-strong: oklch(0.97 0.01 88);
  --muted: oklch(0.7 0.022 174);
  --muted2: oklch(0.58 0.022 174);
  --footer: oklch(0.62 0.024 174);
  --accent: oklch(0.79 0.145 67);
  --accent-soft: oklch(0.32 0.055 67);
  --ok: oklch(0.76 0.14 158);
  --warn: oklch(0.82 0.135 84);
  --crit: oklch(0.72 0.18 31);
  --bar-bg: oklch(0.31 0.018 174);
  --badge-ok-bg: oklch(0.27 0.045 158);
  --badge-warn-bg: oklch(0.29 0.05 84);
  --badge-crit-bg: oklch(0.28 0.055 31);
  --chip-bg: oklch(0.27 0.045 31);
  --logo-bg: oklch(0.25 0.04 160);
  --logo-stroke: oklch(0.48 0.08 160);
  --shadow: 0 18px 50px rgb(0 0 0 / 0.22);
  --grid-wash: rgb(255 255 255 / 0.022);
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg: oklch(0.955 0.009 187);
  --bg-deep: oklch(0.925 0.012 187);
  --panel: oklch(0.985 0.006 187);
  --panel-raised: oklch(0.97 0.012 187);
  --line: oklch(0.84 0.014 187);
  --line-strong: oklch(0.72 0.02 187);
  --fg: oklch(0.24 0.022 174);
  --fg-strong: oklch(0.17 0.02 174);
  --muted: oklch(0.48 0.022 181);
  --muted2: oklch(0.6 0.02 181);
  --footer: oklch(0.54 0.022 181);
  --accent: oklch(0.57 0.17 37);
  --accent-soft: oklch(0.92 0.035 37);
  --ok: oklch(0.5 0.125 157);
  --warn: oklch(0.59 0.135 76);
  --crit: oklch(0.53 0.185 28);
  --bar-bg: oklch(0.88 0.015 187);
  --badge-ok-bg: oklch(0.91 0.04 157);
  --badge-warn-bg: oklch(0.92 0.045 82);
  --badge-crit-bg: oklch(0.92 0.04 28);
  --chip-bg: oklch(0.93 0.03 28);
  --logo-bg: oklch(0.91 0.035 160);
  --logo-stroke: oklch(0.73 0.065 160);
  --shadow: 0 18px 50px rgb(35 58 56 / 0.09);
  --grid-wash: rgb(28 61 58 / 0.035);
}
* { box-sizing: border-box; letter-spacing: 0; }
html { min-width: 320px; }
body {
  min-height: 100vh; margin: 0; padding: 38px 28px 54px;
  font-family: "Avenir Next", Avenir, "Helvetica Neue", sans-serif;
  color: var(--fg);
  background:
    linear-gradient(135deg, var(--grid-wash) 1px, transparent 1px) 0 0 / 18px 18px,
    linear-gradient(45deg, var(--grid-wash) 1px, transparent 1px) 0 0 / 18px 18px,
    linear-gradient(180deg, var(--bg), var(--bg-deep));
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--fg-strong); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.wrap { width: min(100%, 1240px); margin: 0 auto; }
header {
  position: relative; display: flex; flex-wrap: wrap; align-items: flex-end; gap: 22px;
  margin-bottom: 34px; padding: 0 0 24px; border-bottom: 1px solid var(--line-strong);
}
header::after {
  content: ""; position: absolute; left: 0; bottom: -1px; width: 112px; height: 4px;
  background: var(--accent);
}
header h1 {
  margin: 0; color: var(--fg-strong); font-family: "Iowan Old Style", "Palatino Linotype", Baskerville, Georgia, serif;
  font-size: 40px; font-weight: 500; line-height: 0.98;
}
header .sub { margin-top: 8px; color: var(--muted); font-size: 13px; }
.spacer { flex: 1; }
.badge {
  display: inline-flex; align-items: center; gap: 8px; min-height: 38px; padding: 8px 12px;
  border: 1px solid currentColor; border-radius: 3px; background: transparent;
  font-family: "SFMono-Regular", Menlo, Monaco, monospace; font-size: 12px; font-weight: 600;
}
.badge::before { content: ""; width: 7px; height: 7px; background: currentColor; }
.badge.ok { color: var(--ok); background: var(--badge-ok-bg); }
.badge.degraded { color: var(--warn); background: var(--badge-warn-bg); }
.badge.critical { color: var(--crit); background: var(--badge-crit-bg); }
.grid {
  display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 1px;
  overflow: hidden; border: 1px solid var(--line-strong); border-radius: 4px;
  background: var(--line); box-shadow: var(--shadow);
}
.card {
  position: relative; display: flex; flex-direction: column; justify-content: space-between;
  grid-column: span 2; min-height: 112px; padding: 14px 18px;
  background: var(--panel); border: 0; border-radius: 0;
}
.card::after {
  content: ""; position: absolute; top: 13px; right: 16px; width: 22px; height: 1px;
  background: var(--line-strong); box-shadow: 0 5px 0 var(--line-strong);
}
.card:nth-child(1), .card:nth-child(2) { grid-column: span 3; min-height: 138px; }
.card .k {
  color: var(--muted); font-family: "SFMono-Regular", Menlo, Monaco, monospace;
  font-size: 10px; font-weight: 600; text-transform: uppercase;
}
.card .v {
  margin-top: 10px; color: var(--fg-strong);
  font-family: "SFMono-Regular", Menlo, Monaco, monospace;
  font-size: 28px; font-weight: 500; line-height: 1; font-variant-numeric: tabular-nums;
}
.card:nth-child(1) .v, .card:nth-child(2) .v { font-size: 38px; }
.card .u { min-height: 16px; margin-top: 8px; color: var(--muted2); font-size: 12px; }
.val-ok { color: var(--ok); }
.val-warn { color: var(--warn); }
.val-crit { color: var(--crit); }
.val-na { color: var(--muted2); }
.section { margin-top: 48px; }
.section .sub { margin-bottom: 14px; color: var(--muted); font-size: 13px; }
.section h2 {
  display: flex; align-items: center; gap: 14px; margin: 0 0 15px;
  color: var(--fg-strong); font-family: "SFMono-Regular", Menlo, Monaco, monospace;
  font-size: 11px; font-weight: 600; text-transform: uppercase;
}
.section h2::before { content: ""; width: 7px; height: 7px; background: var(--accent); }
.section h2::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.section .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.section .card { grid-column: span 1; min-height: 176px; }
.section .card:nth-child(1), .section .card:nth-child(2) { grid-column: span 1; min-height: 176px; }
.section .card:nth-child(1) .v, .section .card:nth-child(2) .v { font-size: 20px; }
table {
  width: 100%; overflow: hidden; border: 1px solid var(--line);
  border-collapse: separate; border-spacing: 0; border-radius: 4px;
  background: var(--panel); box-shadow: var(--shadow); font-size: 13px;
}
th, td { padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th {
  color: var(--muted); font-family: "SFMono-Regular", Menlo, Monaco, monospace;
  font-size: 10px; font-weight: 600; text-transform: uppercase;
}
td { color: var(--fg); }
tr:last-child td { border-bottom: 0; }
tbody tr { transition: background-color 180ms ease-out; }
tbody tr:hover { background: var(--panel-raised); }
.bar { height: 7px; overflow: hidden; border-radius: 1px; background: var(--bar-bg); }
.bar > span { display: block; height: 100%; border-radius: 1px; }
.bar > span.val-ok { background: var(--ok); }
.bar > span.val-warn { background: var(--warn); }
.bar > span.val-crit { background: var(--crit); }
.dot {
  display: inline-block; width: 8px; height: 8px; margin-right: 8px;
  border-radius: 1px; transform: rotate(45deg);
}
.dot.ok { background: var(--ok); }
.dot.warn { background: var(--warn); }
.dot.crit { background: var(--crit); }
.dot.na { background: var(--muted2); }
.issues { display: flex; flex-wrap: wrap; gap: 9px; }
.chip {
  padding: 7px 11px; border: 1px solid var(--crit); border-radius: 3px;
  background: var(--chip-bg); color: var(--crit); font-size: 12px;
}
footer {
  margin-top: 54px; padding-top: 18px; border-top: 1px solid var(--line);
  color: var(--footer); font-family: "SFMono-Regular", Menlo, Monaco, monospace; font-size: 11px;
}
.brand { display: flex; align-items: center; gap: 18px; }
.brand svg { display: block; flex: 0 0 auto; }
.brand .signal-ring { transform-origin: 22px 22px; animation: signal-pulse 4.5s cubic-bezier(0.16, 1, 0.3, 1) infinite; }
.theme-toggle {
  position: fixed; right: 22px; bottom: 22px; z-index: 20;
  display: grid; width: 92px; height: 92px; place-items: center; padding: 0;
  border: 1px solid var(--line-strong); border-radius: 50%;
  background: var(--panel); color: var(--fg-strong); box-shadow: var(--shadow);
  cursor: pointer; transition: background-color 180ms ease-out, border-color 180ms ease-out, color 180ms ease-out, transform 180ms ease-out;
}
.theme-toggle:hover { transform: translateY(-2px); border-color: var(--accent); color: var(--accent); background: var(--panel-raised); }
.theme-toggle:active { transform: translateY(0); }
.theme-toggle .theme-icon {
  position: absolute; width: 36px; height: 36px; fill: none; stroke: currentColor;
  stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round;
  transition: opacity 180ms ease-out, transform 220ms cubic-bezier(0.16, 1, 0.3, 1);
}
.theme-toggle .theme-icon-sun { opacity: 1; transform: rotate(0) scale(1); }
.theme-toggle .theme-icon-moon { opacity: 0; transform: rotate(-18deg) scale(0.72); }
:root[data-theme="dark"] .theme-toggle .theme-icon-sun { opacity: 0; transform: rotate(18deg) scale(0.72); }
:root[data-theme="dark"] .theme-toggle .theme-icon-moon { opacity: 1; transform: rotate(0) scale(1); }
body, .theme-toggle, .card, table, th, td, .badge, .chip {
  transition: background-color 220ms ease-out, color 220ms ease-out, border-color 220ms ease-out;
}
@keyframes ui-enter {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes signal-pulse {
  0%, 72%, 100% { opacity: 0.35; transform: scale(0.9); }
  80% { opacity: 0.85; transform: scale(1); }
}
header, .grid, .section, footer {
  animation: ui-enter 640ms cubic-bezier(0.16, 1, 0.3, 1) both;
}
.grid { animation-delay: 70ms; }
.section:nth-of-type(1) { animation-delay: 120ms; }
.section:nth-of-type(2) { animation-delay: 160ms; }
.section:nth-of-type(3) { animation-delay: 200ms; }
footer { animation-delay: 240ms; }
@media (max-width: 940px) {
  .grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .card:nth-child(n + 3) { grid-column: span 1; }
}
@media (max-width: 680px) {
  body { padding: 24px 16px 40px; }
  .theme-toggle { right: 16px; bottom: 16px; }
  header { align-items: flex-start; gap: 18px; }
  header h1 { font-size: 32px; }
  .spacer { display: none; }
  .badge { align-self: flex-start; }
  .grid, .section .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .card, .card:nth-child(n + 3), .section .card { grid-column: span 1; min-height: 104px; padding: 14px; }
  .card:nth-child(1), .card:nth-child(2) { grid-column: span 2; min-height: 124px; }
  .card:nth-child(1) .v, .card:nth-child(2) .v { font-size: 34px; }
  .section .card:nth-child(1) .v, .section .card:nth-child(2) .v { font-size: 18px; }
  th, td { padding: 10px 8px; }
  .section { margin-top: 38px; }
}
@media (max-width: 430px) {
  .grid, .section .grid { grid-template-columns: 1fr; }
  .card, .card:nth-child(1), .card:nth-child(2), .section .card { grid-column: span 1; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation: none !important; transition: none !important; }
}
"""

LOGO = """<svg width="44" height="44" viewBox="0 0 44 44" fill="none" xmlns="http://www.w3.org/2000/svg">
  <rect width="44" height="44" rx="11" fill="var(--logo-bg)"/>
  <rect width="44" height="44" rx="11" stroke="var(--logo-stroke)" stroke-width="1.5"/>
  <circle class="signal-ring" cx="22" cy="22" r="13" stroke="var(--accent)" stroke-width="1.25"/>
  <path d="M8 22h6l3-9 6 18 4-13 2 4h7" stroke="var(--ok)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


def temp_class(v):
    if v is None:
        return "na"
    if v > CONFIG["temp_crit"]:
        return "crit"
    if v > CONFIG["temp_warn"]:
        return "warn"
    return "ok"


def pct_class(v):
    if v is None:
        return "na"
    if v > 90:
        return "crit"
    if v > 70:
        return "warn"
    return "ok"


def bar_html(pct, cls):
    pct = max(0.0, min(100.0, pct or 0.0))
    return (
        f'<div class="bar"><span style="width:{pct:.0f}%" '
        f'class="val-{cls}"></span></div>'
    )


WMO_CODES = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Moderate drizzle", "🌦️"),
    55: ("Dense drizzle", "🌦️"),
    56: ("Freezing drizzle", "🌧️"),
    57: ("Freezing drizzle", "🌧️"),
    61: ("Light rain", "🌧️"),
    63: ("Moderate rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"),
    67: ("Freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Moderate snow", "🌨️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Light rain showers", "🌧️"),
    81: ("Rain showers", "🌧️"),
    82: ("Violent rain showers", "⛈️"),
    85: ("Snow showers", "🌨️"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with hail", "⛈️"),
    99: ("Thunderstorm with hail", "⛈️"),
}


def aqi_category(aqi):
    """Return (label, css_class, warning_emoji). The emoji is non-empty only
    for 'Unhealthy' and worse."""
    if aqi is None:
        return "Unknown", "na", ""
    if aqi <= 50:
        return "Good", "ok", ""
    if aqi <= 100:
        return "Moderate", "warn", ""
    if aqi <= 150:
        return "Unhealthy for sensitive groups", "warn", ""
    if aqi <= 200:
        return "Unhealthy", "crit", "💣"
    if aqi <= 300:
        return "Very unhealthy", "crit", "💥"
    return "Hazardous", "crit", "☠️"


def compass_dir(deg):
    if deg is None:
        return ""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int(((deg + 22.5) % 360) / 45)]


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fmt_day(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{WEEKDAYS[dt.weekday()]} {dt.strftime('%m-%d')}"
    except Exception:
        return date_str or "—"


def _num(v, nd=0, suffix=""):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return f"{v}{suffix}"


def render_weather(wd):
    if not wd:
        return ""
    title = '<div class="section"><h2>Weather &amp; Air Quality</h2>'
    if wd.get("loading"):
        return title + '<p style="color:var(--muted)">Loading…</p></div>'
    if not wd.get("available"):
        err = wd.get("error") or "weather unavailable"
        return title + f'<div class="issues"><span class="chip">{err}</span></div></div>'

    loc = wd.get("location") or {}
    loc_name = loc.get("name") or "—"
    wx = wd.get("weather")
    cur = wd.get("current") or {}

    if wx:
        code = wx.get("weather_code")
        if isinstance(code, int):
            cond, emoji = WMO_CODES.get(code, ("Unknown", "🌡️"))
        else:
            cond, emoji = "Unknown", "🌡️"
        temp = _num(wx.get("temperature_2m"))
        feels = _num(wx.get("apparent_temperature"))
        hum = _num(wx.get("relative_humidity_2m"))
        wind = _num(wx.get("wind_speed_10m"), nd=1)
        gust = _num(wx.get("wind_gusts_10m"), nd=1)
        cloud = _num(wx.get("cloud_cover"))
        rain = _num(wx.get("rain"), nd=1)
        wdir = compass_dir(wx.get("wind_direction_10m"))
        weather_card = (
            '<div class="card">'
            '<div class="k">Weather</div>'
            f'<div class="v" style="font-size:18px">{emoji} {cond}</div>'
            f'<div class="u">{temp}°C · feels {feels}°C</div>'
            f'<div class="u">Humidity {hum}% · Wind {wind} km/h {wdir} (gusts {gust})</div>'
            f'<div class="u">Clouds {cloud}% · Rain {rain} mm</div>'
            "</div>"
        )
    else:
        weather_card = (
            '<div class="card"><div class="k">Weather</div>'
            '<div class="u">No weather data</div></div>'
        )

    aqi = cur.get("us_aqi")
    aqi_cat, aqi_cls, aqi_emoji = aqi_category(aqi)
    aqi_show = f"{aqi:.0f}" if isinstance(aqi, (int, float)) else "—"
    aqi_label = f"{aqi_emoji} {aqi_cat}".strip()
    aqi_card = (
        '<div class="card">'
        '<div class="k">Air quality (US AQI)</div>'
        f'<div class="v val-{aqi_cls}" style="font-size:18px">{aqi_show} · {aqi_label}</div>'
        f'<div class="u">PM2.5 {_num(cur.get("pm2_5"), nd=1)} · PM10 {_num(cur.get("pm10"), nd=1)} µg/m³</div>'
        f'<div class="u">O₃ {_num(cur.get("ozone"), nd=1)} · NO₂ {_num(cur.get("nitrogen_dioxide"), nd=1)} · '
        f'SO₂ {_num(cur.get("sulphur_dioxide"), nd=1)} · CO {_num(cur.get("carbon_monoxide"))}</div>'
        "</div>"
    )

    forecast = wd.get("forecast") or []
    forecast_html = ""
    if forecast:
        rows = ""
        for f in forecast:
            code = f.get("code")
            if isinstance(code, int):
                cond, emoji = WMO_CODES.get(code, ("Unknown", "🌡️"))
            else:
                cond, emoji = "Unknown", "🌡️"
            tmax = _num(f.get("tmax"))
            tmin = _num(f.get("tmin"))
            faqi = f.get("aqi")
            faqi_cat, faqi_cls, faqi_emoji = aqi_category(faqi)
            faqi_show = f"{faqi:.0f}" if isinstance(faqi, (int, float)) else "—"
            faqi_label = f"{faqi_emoji} {faqi_cat}".strip()
            rows += (
                f'<tr><td>{fmt_day(f.get("date"))}</td>'
                f'<td>{emoji} {cond}</td>'
                f'<td>{tmin}–{tmax}°C</td>'
                f'<td class="val-{faqi_cls}">{faqi_show} · {faqi_label}</td></tr>'
            )
        forecast_html = (
            '<table style="margin-top:16px">'
            '<thead><tr><th>Day</th><th>Condition</th><th>Temp</th><th>US AQI</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
        )
    elif wd.get("forecast_error"):
        forecast_html = (
            '<div style="margin-top:12px;color:var(--muted);font-size:12px">'
            f'Forecast unavailable: {wd["forecast_error"]}</div>'
        )

    return (
        title
        + f'<div class="sub">{loc_name}</div>'
        + '<div class="grid weather-grid">'
        + weather_card
        + aqi_card
        + "</div>"
        + forecast_html
        + "</div>"
    )


def render_security(sec, f2b):
    if not sec:
        return ""
    sessions = sec.get("active_sessions", 0)
    sources = sec.get("sources") or []
    fails = (sec.get("ssh_failures") or {}).get("failed")
    fw = sec.get("firewall", "unknown")
    fv = sec.get("filevault", "unknown")

    fb = f2b or {}
    if fb.get("status") == "disabled":
        fb_txt, fb_cls = "disabled", "val-warn"
    elif fb.get("pids"):
        fb_txt = "running"
        fb_cls = "val-ok"
    else:
        fb_txt, fb_cls = "down", "val-crit"
    fb_detail = "—"
    if fb.get("banned") is not None:
        fb_detail = f"{fb['banned']} banned"
        if fb.get("jails") is not None:
            fb_detail += f" · {len(fb['jails'])} jails"
    elif fb.get("status") != "disabled":
        fb_detail = "root helper not installed"

    fails_cls = "val-ok" if (fails or 0) == 0 else "val-warn"
    fails_txt = fails if fails is not None else "n/a"
    fw_cls = "val-ok" if fw == "enabled" else "val-warn"
    fv_cls = "val-ok" if fv == "on" else "val-warn"

    rows = (
        f'<tr><td>Active SSH sessions</td><td>{sessions}</td>'
        f'<td>{" · ".join(sources) if sources else "none"}</td></tr>'
        f'<tr><td>fail2ban</td><td class="{fb_cls}">{fb_txt}</td><td>{fb_detail}</td></tr>'
        f'<tr><td>Failed logins (1h)</td><td class="{fails_cls}">{fails_txt}</td>'
        f'<td>sshd, from the unified log</td></tr>'
        f'<tr><td>Firewall</td><td class="{fw_cls}">{fw}</td><td>Application Firewall</td></tr>'
        f'<tr><td>FileVault</td><td class="{fv_cls}">{fv}</td><td>disk encryption</td></tr>'
    )
    return (
        '<div class="section"><h2>Security</h2>'
        '<table><thead><tr><th>Item</th><th>Value</th><th>Detail</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def render_connectivity(conn):
    if not conn:
        return ""
    dns = conn.get("dns", {})
    inet = conn.get("internet", {})
    route = conn.get("route", {})

    def row(label, obj):
        st = obj.get("status", "unknown")
        cls = "val-ok" if st == "ok" else "val-crit"
        detail = obj.get("ip") or obj.get("target") or obj.get("error") or ""
        ms = obj.get("ms")
        if ms is not None:
            detail = (detail + " · " if detail else "") + f"{ms}ms"
        return f'<tr><td>{label}</td><td class="{cls}">{st}</td><td>{detail}</td></tr>'

    rows = row("DNS lookup", dns) + row("Internet (TCP)", inet)
    iface = route.get("interface")
    gw = route.get("gateway")
    tun = route.get("tunneled")
    rcls = "val-warn" if tun else "val-ok"
    rdetail = (iface or "—") + (f" · via {gw}" if gw else "")
    rows += (
        f'<tr><td>Default route</td><td class="{rcls}">'
        f'{"tunnel" if tun else "direct"}</td><td>{rdetail}</td></tr>'
    )
    return (
        '<div class="section"><h2>Connectivity</h2>'
        '<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def render_backup(backup):
    if not backup:
        return ""
    tm = backup.get("time_machine") or {}
    io = backup.get("io") or {}
    disks = io.get("disks") or {}

    latest = tm.get("latest")
    age = tm.get("age_days")
    err = tm.get("error")
    if latest and age is not None:
        cls = "val-ok" if age <= 2 else "val-warn"
        tm_cell = f'<span class="{cls}">{age}d ago</span>'
        tm_detail = latest
    elif err:
        tm_cell = '<span class="val-crit">never</span>'
        tm_detail = err
    else:
        tm_cell = '<span class="val-crit">never</span>'
        tm_detail = "no completed backup found"
    if tm.get("running"):
        tm_detail += " · running now"

    io_txt = "—"
    if disks:
        name, d = max(disks.items(), key=lambda kv: kv[1].get("mb_s", 0))
        io_txt = f'{name}: {d.get("mb_s", 0):.1f} MB/s · {d.get("tps", 0):.0f} tps'

    rows = (
        f'<tr><td>Time Machine</td><td>{tm_cell}</td><td>{tm_detail}</td></tr>'
        f'<tr><td>Disk I/O (busiest)</td><td>—</td><td>{io_txt}</td></tr>'
    )
    return (
        '<div class="section"><h2>Backup &amp; Disk Health</h2>'
        '<table><thead><tr><th>Item</th><th>Status</th><th>Detail</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def render_ports(ports):
    if not ports:
        return ""
    rows = ""
    for p in ports:
        badge = ('<span class="val-warn">LAN</span>' if p.get("exposed")
                 else '<span class="val-ok">local</span>')
        rows += (
            f'<tr><td>{p["port"]}</td><td>{p["command"]}</td>'
            f'<td>{p["address"]}</td><td>{badge}</td></tr>'
        )
    return (
        '<div class="section"><h2>Listening ports</h2>'
        '<table><thead><tr><th>Port</th><th>Process</th><th>Address</th><th>Scope</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def render_html(data):
    sys_ = data.get("system", {})
    hw = data.get("hardware", {})
    mem = hw.get("memory", {})
    msv = data.get("mediasrv", {})
    ssh = data.get("ssh", {})
    ips = data.get("local_ips", [])
    status = data.get("status", "unknown")

    # Fans summary
    fans = hw.get("fans") or []
    fan_txt = "—"
    if fans:
        fan_txt = ", ".join(f"{f.get('name','?')} {f.get('rpm','?')}rpm" for f in fans)
    elif hw.get("available"):
        fan_txt = "none"

    cpu_t = hw.get("cpu_temp_avg")
    gpu_t = hw.get("gpu_temp_avg")
    cpu_u = hw.get("cpu_usage_pct")
    gpu_u = hw.get("gpu_usage_pct")
    ram_used = mem.get("ram_usage")
    ram_total = mem.get("ram_total")
    swap_used = mem.get("swap_usage")
    swap_total = mem.get("swap_total")
    ram_pct = (ram_used / ram_total * 100) if (ram_used and ram_total) else None

    cpu_txt = f"{cpu_t:.1f}°C" if cpu_t is not None else "—"
    gpu_txt = f"{gpu_t:.1f}°C" if gpu_t is not None else "—"
    cpu_utxt = f"{cpu_u*100:.1f}%" if cpu_u is not None else "—"
    gpu_utxt = f"{gpu_u*100:.1f}%" if gpu_u is not None else "—"

    cards = [
        ("CPU temp", cpu_txt, temp_class(cpu_t), None),
        ("GPU temp", gpu_txt, temp_class(gpu_t), None),
        ("CPU usage", cpu_utxt, pct_class(cpu_u*100 if cpu_u is not None else None), None),
        ("GPU usage", gpu_utxt, pct_class(gpu_u*100 if gpu_u is not None else None), None),
        ("Memory", f"{ram_pct:.0f}%" if ram_pct is not None else "—",
         pct_class(ram_pct), f"{fmt_bytes(ram_used)} / {fmt_bytes(ram_total)}"),
        ("Swap", f"{fmt_bytes(swap_used)}", "ok", f"of {fmt_bytes(swap_total)}"),
        ("Power", f"{hw.get('sys_power', 0):.1f}W" if hw.get("sys_power") is not None else "—", "ok", "system"),
        ("Fans", fan_txt[:24], "ok", None),
    ]
    cards_html = ""
    for k, v, cls, u in cards:
        cards_html += (
            f'<div class="card"><div class="k">{k}</div>'
            f'<div class="v val-{cls}">{v}</div>'
            f'<div class="u">{u or ""}</div></div>'
        )

    # Services
    def svc_html(name, obj, detail):
        st = obj.get("status", "down")
        dot = {"ok": "ok", "degraded": "warn", "warn": "warn", "down": "crit", "disabled": "na"}.get(st, "crit")
        return (
            f'<tr><td><span class="dot {dot}"></span>{name}</td>'
            f'<td>{st}</td><td>{detail}</td></tr>'
        )

    msv_detail = []
    if msv.get("pid"):
        msv_detail.append(f"pid {msv['pid']}")
    if msv.get("http_status"):
        msv_detail.append(f"HTTP {msv['http_status']}")
    if msv.get("http_latency_ms") is not None:
        msv_detail.append(f"{msv['http_latency_ms']}ms")
    msv_detail = " · ".join(msv_detail) or "not detected"

    ssh_detail = f"port {ssh.get('port')} open" if ssh.get("port_open") else "port closed"

    fb = data.get("fail2ban", {})
    fb_detail = []
    if fb.get("pids"):
        fb_detail.append(f"pid {fb['pids'][0]}")
    if fb.get("jails") is not None:
        fb_detail.append(f"{len(fb['jails'])} jails")
    if fb.get("banned") is not None:
        fb_detail.append(f"{fb['banned']} banned")
    if fb.get("stale"):
        fb_detail.append("status stale")
    if fb.get("error"):
        fb_detail.append(fb["error"])
    if not fb_detail:
        fb_detail.append("no detail (root helper not installed)")
    fb_detail = " · ".join(fb_detail)

    tn = data.get("tunnel", {})
    tn_detail = []
    if tn.get("pid"):
        tn_detail.append(f"pid {tn['pid']}")
    if tn.get("remote_port"):
        tn_detail.append(f"-R {tn['remote_port']}")
    tn_detail = " · ".join(tn_detail) or "autossh not detected"

    services = (
        svc_html("MediaSrv (media server)", msv, msv_detail)
        + svc_html("SSH (Remote Login)", ssh, ssh_detail)
        + svc_html("fail2ban", fb, fb_detail)
        + svc_html("Reverse SSH tunnel", tn, tn_detail)
    )
    for c in data.get("checks", []):
        services += svc_html(f'{c["name"]} (check)', c, c.get("detail") or "")

    # Disk
    disk_rows = ""
    for v in data.get("disk", []):
        if v.get("error"):
            disk_rows += f'<tr><td>{v["label"]}</td><td colspan="2">{v["error"]}</td></tr>'
            continue
        cls = pct_class(v.get("pct"))
        disk_rows += (
            f'<tr><td>{v["label"]}</td>'
            f'<td style="width:40%">{bar_html(v.get("pct"), cls)}</td>'
            f'<td>{fmt_bytes(v.get("free"))} free / {fmt_bytes(v.get("total"))} '
            f'({v.get("pct")}% used)</td></tr>'
        )

    # Drives & SMART (boot volume + configured external drive)
    drive_rows = ""
    for d in data.get("drives", []):
        label = d.get("label") or "—"
        if not d.get("found"):
            drive_rows += (
                f'<tr><td>{label}</td>'
                f'<td><span class="dot crit"></span>not found</td>'
                f'<td colspan="2">selector {d.get("selector") or "—"}</td>'
                f'<td>—</td></tr>'
            )
            continue
        mount_cell = (
            '<span class="dot ok"></span>mounted'
            if d.get("mounted")
            else '<span class="dot warn"></span>not mounted'
        )
        pct = d.get("pct")
        if pct is not None:
            usage = bar_html(pct, pct_class(pct))
            space = f'{fmt_bytes(d.get("free"))} free / {fmt_bytes(d.get("total"))} ({pct}% used)'
        else:
            usage = "—"
            space = "—"
        smart = d.get("smart") or {}
        st = smart.get("status") or "Unknown"
        low = st.lower()
        scls = "ok" if low.startswith("verified") else ("crit" if low.startswith("fail") else "na")
        dev = d.get("whole_disk") or d.get("device") or "—"
        media = smart.get("media") or ""
        ssd = " · SSD" if smart.get("solid_state") else ""
        smart_cell = (
            f'<span class="val-{scls}">{st}</span>'
            f'<div class="u">{dev}{" · " + media if media else ""}{ssd}</div>'
        )
        drive_rows += (
            f'<tr><td>{label}</td><td>{mount_cell}</td>'
            f'<td style="width:28%">{usage}</td><td>{space}</td><td>{smart_cell}</td></tr>'
        )

    # Top processes
    proc_rows = ""
    for p in data.get("top_processes", []):
        name = p["name"]
        if len(name) > 52:
            name = "…" + name[-51:]
        proc_rows += (
            f'<tr><td>{p["pid"]}</td><td>{name}</td>'
            f'<td>{p["cpu"]}%</td><td>{p["mem"]}%</td></tr>'
        )

    # Load
    load = data.get("loadavg", [0, 0, 0])
    load_txt = " · ".join(f"{x:.2f}" for x in load)

    # Issues
    issues = data.get("issues", [])
    issues_html = "".join(f'<span class="chip">{i}</span>' for i in issues) or (
        '<span style="color:var(--ok)">No issues detected</span>'
    )

    host_line = sys_.get("hostname", "?")
    if ips:
        host_line += " · " + ", ".join(ips)

    collected = data.get("timestamp", "?")

    return f"""<!doctype html>
<html lang="en" data-theme="{CONFIG['theme']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{CONFIG['refresh']}">
<title>My Service · {sys_.get('hostname','')}</title>
<style>{CSS}</style>
<script>
(() => {{
  try {{
    const saved = localStorage.getItem("srvwatch-theme");
    if (saved === "dark" || saved === "light") document.documentElement.dataset.theme = saved;
  }} catch (_) {{}}
}})();
</script>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      {LOGO}
      <div>
        <h1>My Service</h1>
        <div class="sub">{host_line}</div>
      </div>
    </div>
    <div class="spacer"></div>
    <span class="badge {status}">{status.upper()}</span>
  </header>

  <div class="grid">{cards_html}</div>

  <div class="section">
    <h2>Services</h2>
    <table><thead><tr><th>Service</th><th>Status</th><th>Detail</th></tr></thead>
    <tbody>{services}</tbody></table>
  </div>

  {render_security(data.get("security"), data.get("fail2ban"))}

  {render_connectivity(data.get("connectivity"))}

  {render_weather(data.get("weather"))}

  <div class="section">
    <h2>Disks</h2>
    <table><thead><tr><th>Volume</th><th>Usage</th><th>Space</th></tr></thead>
    <tbody>{disk_rows}</tbody></table>
  </div>

  <div class="section">
    <h2>Drives &amp; SMART</h2>
    <table><thead><tr><th>Volume</th><th>Mount</th><th>Usage</th><th>Space</th><th>SMART</th></tr></thead>
    <tbody>{drive_rows}</tbody></table>
  </div>

  {render_backup(data.get("backup"))}

  {render_ports(data.get("ports"))}

  <div class="section">
    <h2>System</h2>
    <table>
      <tr><th style="width:220px">Uptime</th><td>{fmt_uptime(data.get('uptime_seconds'))}</td></tr>
      <tr><th>Load average</th><td>{load_txt}</td></tr>
      <tr><th>OS</th><td>{sys_.get('os','?')} ({sys_.get('os_version','?')})</td></tr>
      <tr><th>Machine</th><td>{sys_.get('machine','?')}</td></tr>
      <tr><th>Python</th><td>{sys_.get('python','?')}</td></tr>
    </table>
  </div>

  <div class="section">
    <h2>Top processes (CPU)</h2>
    <table><thead><tr><th>PID</th><th>Name</th><th>CPU</th><th>Mem</th></tr></thead>
    <tbody>{proc_rows}</tbody></table>
  </div>

  <div class="section">
    <h2>Issues</h2>
    <div class="issues">{issues_html}</div>
  </div>

  <footer>Last collected: {collected} · refresh {CONFIG['refresh']}s · JSON at <a href="/api/status">/api/status</a></footer>
</div>
<button class="theme-toggle" type="button" aria-label="Switch theme">
  <svg class="theme-icon theme-icon-sun" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <circle cx="12" cy="12" r="4"></circle>
    <path d="M12 2v2"></path><path d="M12 20v2"></path>
    <path d="m4.93 4.93 1.41 1.41"></path><path d="m17.66 17.66 1.41 1.41"></path>
    <path d="M2 12h2"></path><path d="M20 12h2"></path>
    <path d="m6.34 17.66-1.41 1.41"></path><path d="m19.07 4.93-1.41 1.41"></path>
  </svg>
  <svg class="theme-icon theme-icon-moon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"></path>
  </svg>
</button>
<script>
(() => {{
  const root = document.documentElement;
  const button = document.querySelector(".theme-toggle");
  if (!button) return;

  function syncThemeButton() {{
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    const label = `Switch to ${{next}} theme`;
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
    button.setAttribute("aria-pressed", root.dataset.theme === "dark" ? "true" : "false");
  }}

  button.addEventListener("click", () => {{
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try {{
      localStorage.setItem("srvwatch-theme", next);
    }} catch (_) {{}}
    syncThemeButton();
  }});

  syncThemeButton();
}})();
</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "SrvWatch/1.0"

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self):
        """Return the current snapshot with the freshest weather attached,
        so weather/AQI shows up right away regardless of the collection cycle."""
        data = get_snapshot() or collect_all()
        data = dict(data)
        data["weather"] = get_weather()
        return data

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            data = self._snapshot()
            self._send(200, render_html(data).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status" or path == "/json":
            data = self._snapshot()
            self._send(
                200,
                json.dumps(data, indent=2, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )
        elif path == "/health":
            data = get_snapshot()
            status = data.get("status") if data else "unknown"
            code = 200 if status in ("ok", "degraded") else 503
            self._send(
                code,
                json.dumps({"status": status}).encode("utf-8"),
                "application/json; charset=utf-8",
            )
        else:
            self._send(404, b'{"error": "not found"}', "application/json")

    def log_message(self, fmt, *args):
        # Keep the console quiet: only log non-200 responses.
        if not (len(args) >= 2 and args[1].startswith("200")):
            super().log_message(fmt, *args)


def main():
    # Warm up immediately so the first request already has data.
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()

    server = ThreadingHTTPServer((CONFIG["host"], CONFIG["port"]), Handler)
    addrs = get_local_ips() or [CONFIG["host"]]
    print(f"SrvWatch listening on http://{CONFIG['host']}:{CONFIG['port']}", flush=True)
    for ip in addrs:
        print(f"  -> http://{ip}:{CONFIG['port']}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
