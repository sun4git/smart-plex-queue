#!/bin/bash
# Smart Plex Queue Listener + Dashboard - Restart Script
# Usage: ./restart.sh  or  bash restart.sh
#
# PID files are self-managed: queue_listener.py and dashboard.py each write
# their own PID under ../logs/ on startup and remove it on clean shutdown.
# This script only ever reads them to find what to stop - it never writes
# to them itself, so there's exactly one source of truth per process.

APP_DIR="."
PID_FILE="../logs/queue_listener.pid"
DASHBOARD_PID_FILE="../logs/dashboard.pid"
# Same path dashboard.py itself uses when it restarts the listener from the
# UI, so the console log ends up in one place regardless of which restart
# path triggered it.
CONSOLE_LOG="../logs/smart-queue-console.log"
DASHBOARD_CONSOLE_LOG="../logs/dashboard-console.log"
# Same file queue_listener.py and dashboard.py both read/write - see
# CONFIG_FILE in either of those.
CONFIG_FILE="../.smart-queue-config.json"

# Read one string key out of the shared JSON config, or print nothing if
# the file/key is missing. Uses python3 (already required) instead of
# fragile shell JSON parsing.
get_config_value() {
    local key="$1"
    if [ -f "$CONFIG_FILE" ]; then
        python3 - "$CONFIG_FILE" "$key" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        cfg = json.load(f)
    print(cfg.get(sys.argv[2]) or "")
except Exception:
    print("")
PYEOF
    fi
}

# smart-queue.log's location can be overridden via the config file -
# resolve the real one so the message below doesn't lie about where it is.
# dashboard.log always lives in the same directory (see
# get_dashboard_log_file() in dashboard.py) even though it's a separate
# file, so derive its path from wherever smart-queue.log ended up too.
ACTIVITY_LOG=$(get_config_value "log_file")
if [ -z "$ACTIVITY_LOG" ]; then
    ACTIVITY_LOG="../logs/smart-queue.log"
fi
DASHBOARD_ACTIVITY_LOG="$(dirname "$ACTIVITY_LOG")/dashboard.log"

stop_by_pidfile() {
    local pid_file="$1"
    local label="$2"
    if [ -f "$pid_file" ]; then
        local old_pid
        old_pid=$(cat "$pid_file")
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            echo "Stopping $label (PID: $old_pid)..."
            kill "$old_pid" 2>/dev/null
            sleep 1
        fi
    fi
}

# Kill existing listener if running
stop_by_pidfile "$PID_FILE" "queue listener"

# Also kill any stray processes (e.g. started without writing a PID file)
pkill -f "queue_listener.py" 2>/dev/null
sleep 1

# Start the listener
echo "Starting queue listener..."
cd "$APP_DIR"
mkdir -p ../logs
nohup python3 -u queue_listener.py > "$CONSOLE_LOG" 2>&1 &
NEW_PID=$!
sleep 2

# Verify it's running
if ps -p $NEW_PID > /dev/null 2>&1; then
    echo "✅ Queue listener started successfully (PID: $NEW_PID)"
    echo "📋 Console log: $CONSOLE_LOG"
    echo "📋 Activity log: $ACTIVITY_LOG"
else
    echo "❌ Failed to start queue listener"
    exit 1
fi

# --- Dashboard (UI/control layer - separate process, same restart cycle) ---

stop_by_pidfile "$DASHBOARD_PID_FILE" "dashboard"
pkill -f "dashboard.py" 2>/dev/null
sleep 1

echo "Starting dashboard..."
nohup python3 -u dashboard.py > "$DASHBOARD_CONSOLE_LOG" 2>&1 &
NEW_DASHBOARD_PID=$!
sleep 1

if ps -p $NEW_DASHBOARD_PID > /dev/null 2>&1; then
    echo "✅ Dashboard started successfully (PID: $NEW_DASHBOARD_PID)"
    echo "📋 Console log: $DASHBOARD_CONSOLE_LOG"
    echo "📋 Activity log: $DASHBOARD_ACTIVITY_LOG"
else
    echo "❌ Failed to start dashboard"
    exit 1
fi
