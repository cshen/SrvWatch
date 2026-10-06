# SrvWatch

A tiny, self-contained server-health monitor for the Mac mini. It watches the
machine's vitals and exposes a dashboard at **http://\<IP\>:9001** — no
dependencies beyond the Python standard library and the already-installed
[`macmon`](https://github.com/vladkens/macmon) CLI.

## What it monitors

| Area | Source | Detail |
|------|--------|--------|
| CPU temperature | `macmon pipe` | `temp.cpu_temp_avg` |
| GPU temperature | `macmon pipe` | `temp.gpu_temp_avg` |
| CPU / GPU utilization | `macmon pipe` | per-cluster usage, overall % |
| Fans | `macmon pipe` | fan name + RPM |
| Power | `macmon pipe` | system / total watts |
| Memory & swap | `macmon pipe` | used / total |
| **MediaSrv** | process + TCP :9000 + HTTP 200 | the media server |
| **SSH** (Remote Login) | TCP :22 + sshd processes | connectivity |
| **fail2ban** | process + optional status file | brute-force protection liveness (jails/bans with the root helper) |
| **Reverse SSH tunnel** | `autossh` process + `-R` forward | remote access via `~/bin/ssh_reverse.sh` |
| External drive & SMART | `diskutil` | mount presence, capacity, SMART health |
| SSH security | `who`, `log show`, `fdesetup`, firewall | active sessions, failed logins (1h), firewall, FileVault |
| Connectivity | DNS + TCP + `route` | DNS lookup, internet reachability, default route / VPN |
| Backup | `tmutil` | Time Machine last backup + age |
| Disk I/O | `iostat` | busiest disk throughput |
| Listening ports | `lsof` | TCP listeners, LAN-exposed vs local |
| Configurable checks | `[checks]` in config.toml | process / port / URL probes for any service |
| Weather & air quality | `~/bin/aqi.sh -j` | current condition, temp, wind, rain, US AQI + pollutants |
| 2-day forecast | Open-Meteo (via aqi.sh's coordinates) | daily temp, condition, US AQI |
| Disk | `shutil.disk_usage` | `/` + mounted `/Volumes/*` |
| Load average / uptime | `os.getloadavg` / `sysctl` | — |
| Top CPU processes | `ps` | PID / name / CPU / mem |
| Network | `ipconfig getifaddr` | local IP(s) |

Overall status is derived automatically: **ok** / **degraded** / **critical**,
with the specific issues listed on the page.

## Run (manual)

```sh
./run.sh
# or directly:
python3 srvwatch.py
```

Then open http://localhost:9001 (or http://\<your-IP\>:9001 from another device
on the LAN).

## Install as a background service

Runs on boot and restarts automatically via launchd (`KeepAlive`):

```sh
./deploy/install-launchd.sh install
```

Logs go to `~/Library/Logs/SrvWatch.log`. To stop/remove:

```sh
./deploy/install-launchd.sh uninstall
```

### Optional: fail2ban jail/ban counts

fail2ban's socket and log are root-only, so the unprivileged monitor can only
confirm the process is alive. To also show jail names and current ban counts,
install the small root helper (refreshes every 60s):

```sh
./deploy/install-launchd.sh install-fail2ban     # asks for your password
./deploy/install-launchd.sh uninstall-fail2ban   # remove it
```

## HTTP endpoints

| Path | Description |
|------|-------------|
| `/` | HTML dashboard (auto-refreshes every few seconds) |
| `/api/status` | Full JSON snapshot |
| `/health` | Liveness probe → `{"status": "ok"}` (HTTP 200 when ok/degraded, 503 when critical) |

## Configuration

Settings are resolved in this order (later wins):

1. built-in defaults
2. a `config.toml` file — looked up in: `$SRVWATCH_CONFIG`, then
   `./config.toml` (next to `srvwatch.py`), then `~/.config/SrvWatch/config.toml`
3. `SRVWATCH_*` environment variables

Copy `config.example.toml` to `config.toml` and edit. Everything is optional;
unlisted keys fall back to defaults. A full `config.toml`:

```toml
[server]
host = "0.0.0.0"     # 0.0.0.0 = reachable from other devices on the LAN
port = 9001

[monitor]
refresh = 5          # seconds between collections (5, 10, 20, 30 ...)

[theme]
theme = "dark"       # "dark" or "light"

[macmon]
path = "/opt/homebrew/bin/macmon"

[mediasrv]
port = 9000
url = "http://127.0.0.1:9000/"
pattern = "mediasrv"

[ssh]
port = 22

[fail2ban]
enabled = true          # false = hide from the dashboard
pattern = "fail2ban"    # process-name pattern matched with pgrep -f
status_file = "~/Library/Logs/SrvWatch/fail2ban-status.json"  # from the root helper

[tunnel]
enabled = true
pattern = "autossh"     # process-name pattern
remote_port = 2292      # require this -R forward; omit for "any"

[drive]
label = "MyDrive"                 # display name
mount = "/Volumes/MyDrive"        # expected mount point
device = "disk6s1"                # diskutil selector, works while unmounted
required = false                  # true = raise an issue when absent

[weather]
command = "~/bin/aqi.sh -j"   # prints weather + AQI as JSON on stdout
interval = 3600               # seconds between weather refreshes
timeout = 30                  # seconds to allow the command to run
forecast_days = 2             # future days in the compact forecast table (0 = hide)

[thresholds]
temp_warn = 70.0     # deg C
temp_crit = 85.0     # deg C
disk_warn = 80.0     # percent used
disk_crit = 90.0     # percent used

[checks]
# Extra services to watch:  name = "type:target"
#   process:<pgrep -f pattern>      e.g. process:ollama
#   port:<tcp port on 127.0.0.1>    e.g. port:11434
#   url:<http url>                  e.g. url:http://127.0.0.1:8080/health
# ollama = "port:11434"
```

### Environment variables (override config.toml)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SRVWATCH_PORT` | `9001` | Listen port |
| `SRVWATCH_HOST` | `0.0.0.0` | Bind address |
| `SRVWATCH_REFRESH` | `5` | Collection interval (seconds) |
| `SRVWATCH_THEME` | `dark` | `dark` or `light` |
| `SRVWATCH_MACMON` | `macmon` on PATH | Path to the macmon binary |
| `SRVWATCH_MEDIASRV_PORT` | `9000` | MediaSrv port to check |
| `SRVWATCH_MEDIASRV_URL` | `http://127.0.0.1:9000/` | MediaSrv health URL |
| `SRVWATCH_MEDIASRV_PATTERN` | `mediasrv` | Process-name pattern |
| `SRVWATCH_SSH_PORT` | `22` | SSH port to check |
| `SRVWATCH_FAIL2BAN_ENABLED` | `true` | Monitor fail2ban |
| `SRVWATCH_FAIL2BAN_PATTERN` | `fail2ban` | fail2ban process pattern |
| `SRVWATCH_FAIL2BAN_STATUS_FILE` | `~/Library/Logs/SrvWatch/fail2ban-status.json` | Root-helper status file |
| `SRVWATCH_TUNNEL_ENABLED` | `true` | Monitor the reverse SSH tunnel |
| `SRVWATCH_TUNNEL_PATTERN` | `autossh` | Tunnel process pattern |
| `SRVWATCH_TUNNEL_REMOTE_PORT` | — | Required `-R` forward port |
| `SRVWATCH_DRIVE_LABEL` | — | Drive display name |
| `SRVWATCH_DRIVE_MOUNT` | — | Expected mount point |
| `SRVWATCH_DRIVE_DEVICE` | — | diskutil selector for SMART |
| `SRVWATCH_DRIVE_REQUIRED` | `false` | Raise an issue when the drive is absent |
| `SRVWATCH_WEATHER_COMMAND` | `~/bin/aqi.sh -j` | Command that prints weather/AQI JSON |
| `SRVWATCH_WEATHER_INTERVAL` | `3600` | Weather refresh interval (seconds) |
| `SRVWATCH_WEATHER_TIMEOUT` | `30` | Weather command timeout (seconds) |
| `SRVWATCH_WEATHER_FORECAST_DAYS` | `2` | Future days in the forecast table |

Changes take effect on restart:

```sh
./deploy/install-launchd.sh install   # re-install / restart
# or, when running manually, just restart ./run.sh
```

## Why stdlib-only?

The monitor's whole job is to tell you when the box is unhealthy. It shouldn't
itself depend on a venv, package manager, or anything that can break. `macmon`
is the one external tool, and it's read defensively (a missing/broken `macmon`
just shows "unavailable" rather than crashing the monitor).
