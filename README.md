# Smart Plex Queue

Intelligent song queuing system that listens for Plex webhooks and automatically generates next-track recommendations using MediaSage's API.

## What It Does

When a song finishes on your Plex player, this service:
1. Receives the track-change webhook from Plex
2. Generates a smart playlist suggestion via MediaSage API
3. Queues the selected track(s) back into Plex

The smart queue can be toggled on/off via a flag file. When enabled, webhooks are processed; when disabled, they're logged but ignored.

## Project Layout

```
smart-plex-queue/
├── queue_listener.py            # Main webhook listener service (port 8000)
├── queue_control.py             # CLI control interface
├── dashboard.py                 # Web UI dashboard (port 8001) - control layer only
├── dashboard/                   # Dashboard static assets (HTML/CSS/JS)
├── restart.sh                   # Hard-restart script (stops + starts)
├── README.md                    # This file
```

## Web Dashboard

A browser dashboard for enabling/disabling the feature, watching the log
live, and restarting the listener - without needing the CLI.

```bash
python3 dashboard.py            # Defaults to port 8001
```

Then open `http://localhost:8001/` (or `http://<host>:8001/` on your LAN).

From the dashboard you can enable/disable the feature, watch the log live,
restart the listener, and edit **Listener Settings** (MediaSage server URL,
log file path) - saved to a shared config file both processes read.

The dashboard is a pure UI/control layer: it never imports or changes the
webhook/recommendation logic in `queue_listener.py`. It talks to the same
toggle file, PID file, and log file the listener manages itself, and starts/
stops that *process* the same way `restart.sh` does.

**Security note:** the dashboard has no authentication - anyone who can
reach its port can enable/disable the feature or restart the listener. It
binds to `0.0.0.0` by default (reachable from your LAN); set
`DASHBOARD_HOST=127.0.0.1` if you want to restrict it to just this machine,
and never expose this port to the internet.

| Env var | Default | Description |
|---------|---------|--------------|
| `DASHBOARD_HOST` | `0.0.0.0` | Interface the dashboard binds to |
| `QUEUE_LISTENER_PORT` | `8000` | Port the dashboard expects/starts the listener on |

`restart.sh` restarts both the listener and the dashboard together.

## Shared Config File

`../.smart-queue-config.json` (workspace root, untracked - same convention
as the toggle file) holds settings both the dashboard and the listener care
about:

```json
{
  "mediasage_host": "http://192.168.1.100:5765/api",
  "log_file": "../logs/smart-queue.log"
}
```

Both keys are optional - omit the file entirely, or omit either key, and
`queue_listener.py` falls back to its built-in defaults (the same ones it
always had). You can edit this file by hand or through the dashboard's
Listener Settings card; either way, only `queue_listener.py` reads it, and
only at startup, so a change takes effect on the next restart. The
`MEDIASAGE_HOST` environment variable still overrides the config file if
you set one (so `restart-queue-listener.bat` etc. keep working unchanged).

## Log Files

Everything lives under `../logs/` (gitignored, created automatically):

| File | Written by | What's in it |
|------|-----------|---------------|
| `smart-queue.log` | `queue_listener.py` | Structured webhook/recommendation activity - what the dashboard's log view tails |
| `dashboard.log` | `dashboard.py` | Dashboard-triggered actions: enable/disable, restart requests, settings saves, failed connection tests |
| `smart-queue-console.log` | whatever starts the listener (`restart.sh` or the dashboard's Restart button) | Raw stdout/stderr from the listener process - crash tracebacks, anything printed before logging is set up |
| `dashboard-console.log` | `restart.sh` | Raw stdout/stderr from the dashboard process |

`smart-queue.log`'s location can be overridden via the shared config file
below (`log_file`). `dashboard.log` always lives in the same *directory* as
`smart-queue.log` - even after an override - though it stays a separate
file, since the listener and dashboard are independent processes and
writing both into one file would need cross-process coordination neither
has. The two console logs are always at fixed paths.

## PID Files

`queue_listener.py` and `dashboard.py` each write their own PID to
`../logs/queue_listener.pid` and `../logs/dashboard.pid` on startup, and
remove it on a clean shutdown (SIGTERM/SIGINT). `restart.sh` and the
dashboard's Restart button only ever *read* these files to know what to
stop - nothing else writes to them, so there's one source of truth per
process instead of each script tracking PIDs independently. If a file is
missing or stale (e.g. a crash that skipped cleanup, or a listener started
before this existed), the dashboard falls back to finding the process by
its port.

## Quick Commands

All commands should be run from this directory:

### Control the Service

| Command | Action |
|---------|--------|
| `bash restart.sh` | Stop existing process & start fresh |
| `python3 queue_control.py start` | Start listener (if not running) |
| `python3 queue_control.py stop` | Stop listener |
| `python3 queue_control.py status` | Show enabled/running state |

### Enable / Disable Smart Queue

| Command | Action |
|---------|--------|
| `python3 queue_control.py enable` | Enable auto-queueing |
| `python3 queue_control.py disable` | Disable auto-queueing |
| `python3 queue_control.py toggle` | Flip current state |

When **enabled**, Plex webhooks trigger MediaSage playlist generation.
When **disabled**, webhooks are logged but no tracks are queued.

## Status Check

```bash
cd /home/suneel/sunwork/apps/smart-plex-queue
python3 queue_control.py status
```

Example output:
```
🟢 Smart Queue: ENABLED
🟢 Listener: RUNNING
   PID: 181397
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIASAGE_HOST` | `http://192.168.1.100:5765/api` | MediaSage API base URL |

## Ports

- **8000** — Webhook listener service

## Integration Flow

```
Plex Server → (Webhook) → queue_listener.py (port 8000)
                                ↓ (if enabled)
                        MediaSage API
                                ↓
                        Plex Player (queued track)
```

## Troubleshooting

**Listener won't start:**
- Check log file for errors
- Verify port 8000 isn't already in use (`lsof -i :8000`)
- Ensure `python3` is available

**No tracks being queued:**
- Confirm queue is enabled: `python3 queue_control.py status`
- Verify MediaSage API is reachable at the configured host
- Check log file for webhook receipt / processing errors
