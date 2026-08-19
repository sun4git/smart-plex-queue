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
├── restart.sh                   # Hard-restart script (stops + starts)
├── README.md                    # This file
```

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
