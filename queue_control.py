#!/usr/bin/env python3
"""
Smart Plex Queue Control CLI

Usage:
    python3 queue_control.py --enable      # Enable smart queue
    python3 queue_control.py --disable     # Disable smart queue
    python3 queue_control.py --status      # Check status
    python3 queue_control.py --toggle      # Toggle state
    python3 queue_control.py --start       # Start listener service
    python3 queue_control.py --stop        # Stop listener service

Also supports natural language commands:
    python3 queue_control.py enable
    python3 queue_control.py disable
    python3 queue_control.py status
    python3 queue_control.py start
    python3 queue_control.py stop
"""

import os
import sys
import subprocess
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.dirname(SCRIPT_DIR) # This reliably points to the parent folder

CODE_DIR = os.path.join(WORKSPACE_DIR, "smart-plex-queue")
TOGGLE_FILE = os.path.join(WORKSPACE_DIR, ".smart-queue-enabled")
PID_FILE = "/tmp/smart_plex_queue.pid"

def is_enabled():
    """Check if smart queue is enabled."""
    return os.path.exists(TOGGLE_FILE)

def enable():
    """Enable the smart queue feature."""
    os.makedirs(os.path.dirname(TOGGLE_FILE), exist_ok=True)
    with open(TOGGLE_FILE, "w") as f:
        f.write(f"Smart queue enabled at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    print("✅ Smart Plex Queue ENABLED")
    print("Next song will be automatically queued based on current track context.")

def disable():
    """Disable the smart queue feature."""
    if os.path.exists(TOGGLE_FILE):
        os.remove(TOGGLE_FILE)
    print("❌ Smart Plex Queue DISABLED")
    print("Webhooks will be logged but tracks won't be queued.")

def status():
    """Print current status."""
    enabled = is_enabled()
    listener_running = is_listener_running()

    state_emoji = "🟢" if enabled else "⚪"
    listener_emoji = "🟢" if listener_running else "🔴"

    print(f"{state_emoji} Smart Queue: {'ENABLED' if enabled else 'DISABLED'}")
    print(f"{listener_emoji} Listener: {'RUNNING' if listener_running else 'NOT RUNNING'}")

    if listener_running:
        pid = get_listener_pid()
        print(f"   PID: {pid}")
    else:
        print("   Start with: python3 smart-plex-queue/queue_listener.py &")

def toggle():
    """Toggle the smart queue feature."""
    if is_enabled():
        disable()
    else:
        enable()

def is_listener_running():
    """Check if the listener process is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "queue_listener.py"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except Exception:
        return False

def get_listener_pid():
    """Get the PID of the running listener."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "queue_listener.py"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip().split('\n')[0]
    except Exception:
        pass
    return None

def start_listener():
    """Start the listener service in the background."""
    listener_path = os.path.join(CODE_DIR, "queue_listener.py")
    if is_listener_running():
        print(" Listener already running")
        return

    try:
        subprocess.Popen(
            ["python3", listener_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        time.sleep(1)
        if is_listener_running():
            print("🚀 Listener started successfully")
        else:
            print("⚠️  Listener failed to start - check logs")
    except Exception as e:
        print(f"⚠️  Failed to start listener: {e}")

def stop_listener():
    """Stop the listener service."""
    try:
        subprocess.run(["pkill", "-f", "queue_listener.py"], capture_output=True)
        time.sleep(1)
        if not is_listener_running():
            print("🛑 Listener stopped")
        else:
            print("⚠️  Failed to stop listener")
    except Exception as e:
        print(f"⚠️  Error stopping listener: {e}")

def main():
    if len(sys.argv) < 2:
        print("Usage: queue_control.py [enable|disable|status|toggle|start|stop]")
        print("       Use --enable/--disable/--status/--toggle/--start/--stop for flags")
        sys.exit(1)

    cmd = sys.argv[1].lower().lstrip('-')

    if cmd in ["enable", "on"]:
        enable()
    elif cmd in ["disable", "off"]:
        disable()
    elif cmd in ["toggle", "switch"]:
        toggle()
    elif cmd in ["status", "check"]:
        status()
    elif cmd in ["start"]:
        start_listener()
    elif cmd in ["stop"]:
        stop_listener()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: queue_control.py [enable|disable|status|toggle|start|stop]")
        sys.exit(1)

if __name__ == "__main__":
    main()
