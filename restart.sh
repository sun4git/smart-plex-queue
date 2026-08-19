#!/bin/bash
# Smart Plex Queue Listener - Restart Script
# Usage: ./restart.sh  or  bash restart.sh

APP_DIR="."
PID_FILE="/tmp/smart_plex_queue.pid" # $APP_DIR/queue_listener.pid

# Kill existing process if running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping queue listener (PID: $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# Also kill any stray processes
pkill -f "queue_listener.py" 2>/dev/null
sleep 1

# Start the listener
echo "Starting queue listener..."
cd "$APP_DIR"
nohup python3 -u queue_listener.py > /tmp/smart_queue_console.log 2>&1 &
NEW_PID=$!
echo $NEW_PID > "$PID_FILE"
sleep 2

# Verify it's running
if ps -p $NEW_PID > /dev/null 2>&1; then
    echo "✅ Queue listener started successfully (PID: $NEW_PID)"
    echo "📋 Log: /tmp/smart_queue_console.log"
else
    echo "❌ Failed to start queue listener"
    exit 1
fi
