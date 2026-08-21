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
nohup python3 -u queue_listener.py > /tmp/smart_queue_console.log 2>&1 &
NEW_PID=$!
sleep 2

# Verify it's running
if ps -p $NEW_PID > /dev/null 2>&1; then
    echo "✅ Queue listener started successfully (PID: $NEW_PID)"
    echo "📋 Log: /tmp/smart_queue_console.log"
else
    echo "❌ Failed to start queue listener"
    exit 1
fi

# --- Dashboard (UI/control layer - separate process, same restart cycle) ---

stop_by_pidfile "$DASHBOARD_PID_FILE" "dashboard"
pkill -f "dashboard.py" 2>/dev/null
sleep 1

echo "Starting dashboard..."
nohup python3 -u dashboard.py > /tmp/smart_queue_dashboard_console.log 2>&1 &
NEW_DASHBOARD_PID=$!
sleep 1

if ps -p $NEW_DASHBOARD_PID > /dev/null 2>&1; then
    echo "✅ Dashboard started successfully (PID: $NEW_DASHBOARD_PID)"
    echo "📋 Log: /tmp/smart_queue_dashboard_console.log"
else
    echo "❌ Failed to start dashboard"
    exit 1
fi
