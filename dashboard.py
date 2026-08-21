#!/usr/bin/env python3
"""
Smart Plex Queue - Dashboard (UI layer)

A standalone control-panel web UI for queue_listener.py. This module is a
presentation/control layer only: it never imports or modifies the webhook
handling / recommendation logic in queue_listener.py. It talks to the same
toggle file, PID file, and log file that queue_listener.py manages itself,
and it can start/stop that *process* the same way restart.sh does (read its
PID file to find what to stop, then spawn a fresh `python queue_listener.py`
subprocess, which writes its own new PID file on the way up).

Usage:
    python3 dashboard.py [PORT]        # Default port: 8001

Environment Variables:
    DASHBOARD_HOST         - Host to bind the dashboard to (default: 0.0.0.0,
                              i.e. reachable from your LAN). The dashboard can
                              enable/disable/restart the listener with no
                              authentication, so only run it on a trusted
                              network - set DASHBOARD_HOST=127.0.0.1 to
                              restrict it to just this machine.
    QUEUE_LISTENER_PORT     - Port queue_listener.py runs/should run on
                              (default: 8000).
    MEDIASAGE_HOST          - Same env var queue_listener.py itself reads;
                              if set, it wins over whatever is saved in the
                              shared config file (.smart-queue-config.json).
"""

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# --- Paths (mirrors queue_listener.py's own path conventions) ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.dirname(SCRIPT_DIR)
STATIC_DIR = os.path.join(SCRIPT_DIR, "dashboard")

TOGGLE_FILE = os.path.join(WORKSPACE_DIR, ".smart-queue-enabled")
DEFAULT_LOG_FILE = os.path.join(WORKSPACE_DIR, "logs", "smart-queue.log")
CONSOLE_LOG_FILE = os.path.join(WORKSPACE_DIR, "logs", "smart-queue-console.log")
LISTENER_SCRIPT = os.path.join(SCRIPT_DIR, "queue_listener.py")

# Self-managed PID files - queue_listener.py and this script each write
# their own on startup and remove it on clean shutdown. Nobody else writes
# to these; readers (restart.sh, the code below) only ever read them to
# decide what to stop, so there's exactly one source of truth per process.
LISTENER_PID_FILE = os.path.join(WORKSPACE_DIR, "logs", "queue_listener.pid")
DASHBOARD_PID_FILE = os.path.join(WORKSPACE_DIR, "logs", "dashboard.pid")

# Settings shared with queue_listener.py (MediaSage host, log file location).
# This file is the one thing the dashboard *does* write on the listener's
# behalf - queue_listener.py only ever reads it, at startup.
CONFIG_FILE = os.path.join(WORKSPACE_DIR, ".smart-queue-config.json")
# Keep in sync with queue_listener.py's own MEDIASAGE_API default.
DEFAULT_MEDIASAGE_HOST = "http://192.168.1.100:5765/api"

LISTENER_PORT = int(os.getenv("QUEUE_LISTENER_PORT", "8000"))
IS_WINDOWS = platform.system() == "Windows"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


# --- Toggle file (feature enable/disable) ---

def is_enabled():
    return os.path.exists(TOGGLE_FILE)


def set_enabled(enabled):
    if enabled:
        os.makedirs(os.path.dirname(TOGGLE_FILE), exist_ok=True)
        with open(TOGGLE_FILE, "w") as f:
            f.write(f"Smart queue enabled at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    else:
        if os.path.exists(TOGGLE_FILE):
            os.remove(TOGGLE_FILE)


# --- Shared config (MediaSage server host, log file location) ---

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_mediasage_host():
    """Effective MediaSage host: dashboard config > inherited env > built-in default."""
    return (
        load_config().get("mediasage_host")
        or os.getenv("MEDIASAGE_HOST")
        or DEFAULT_MEDIASAGE_HOST
    )


def get_log_file():
    """Effective structured-log path: dashboard config > built-in default."""
    return load_config().get("log_file") or DEFAULT_LOG_FILE


def is_valid_mediasage_host(value):
    if not isinstance(value, str):
        return False
    value = value.strip()
    return bool(value) and " " not in value and value.lower().startswith(("http://", "https://"))


def test_mediasage_connection(host):
    """GET {host}/health and report reachability. Returns (ok, status_code, detail)."""
    url = host.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return True, resp.status, data
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            data = {}
        data.setdefault("error", f"HTTP {e.code}")
        return False, e.code, data
    except urllib.error.URLError as e:
        return False, None, {"error": str(e.reason)}
    except Exception as e:
        return False, None, {"error": str(e)}


# --- Listener process control (cross-platform: Windows + Linux/macOS) ---

def is_port_open(port, host="127.0.0.1", timeout=0.35):
    """Cheap liveness check: can we open a TCP connection to the listener?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_pid_file(path):
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def is_pid_alive(pid):
    if not pid:
        return False
    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"], text=True, stderr=subprocess.DEVNULL
            )
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except Exception:
        return False


def get_listener_pid():
    """Prefer the PID file the listener writes itself; fall back to
    scanning the port if it's missing or stale (e.g. a listener started
    before this file existed, or a hard crash that skipped cleanup)."""
    pid = read_pid_file(LISTENER_PID_FILE)
    if pid and is_pid_alive(pid):
        return pid
    return get_pid_on_port(LISTENER_PORT)


def get_pid_on_port(port):
    """Fallback lookup of the PID bound to `port`. Returns None if unknown."""
    try:
        if IS_WINDOWS:
            out = subprocess.check_output(
                ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL
            )
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        return int(parts[-1])
        else:
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f"tcp:{port}"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                if out:
                    return int(out.splitlines()[0])
            except (FileNotFoundError, subprocess.CalledProcessError):
                out = subprocess.check_output(
                    ["fuser", f"{port}/tcp"], text=True, stderr=subprocess.DEVNULL
                ).strip()
                if out:
                    return int(out.split()[0])
    except Exception:
        return None
    return None


def kill_pid(pid):
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def stop_listener(port):
    pid = get_listener_pid()
    if pid:
        kill_pid(pid)
    for _ in range(20):
        if not is_port_open(port):
            return True
        time.sleep(0.2)
    return not is_port_open(port)


def start_listener(port):
    os.makedirs(os.path.dirname(CONSOLE_LOG_FILE), exist_ok=True)
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    # No need to inject MEDIASAGE_HOST here - queue_listener.py reads
    # CONFIG_FILE itself at startup, and env still wins if the caller's
    # shell already exported one.

    console_log = open(CONSOLE_LOG_FILE, "a")
    kwargs = dict(
        cwd=SCRIPT_DIR,
        env=env,
        stdout=console_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([sys.executable, "-u", LISTENER_SCRIPT, str(port)], **kwargs)

    for _ in range(25):
        if is_port_open(port):
            return True
        time.sleep(0.2)
    return is_port_open(port)


def restart_listener(port):
    stop_listener(port)
    time.sleep(0.3)
    started = start_listener(port)
    return started


# --- Log tailing ---

def tail_log(path, max_lines=300):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [line.rstrip("\n") for line in lines[-max_lines:]]
    except Exception as e:
        return [f"[dashboard] could not read log: {e}"]


# --- HTTP handler ---

class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SmartQueueDashboard/1.0"

    def log_message(self, format, *args):
        pass  # keep console quiet; queue_listener.py already logs its own activity

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send_static(self, filename, content_type):
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            self._send_static(filename, content_type)
            return

        if path == "/api/status":
            running = is_port_open(LISTENER_PORT)
            self._send_json(
                {
                    "enabled": is_enabled(),
                    "running": running,
                    "port": LISTENER_PORT,
                    "log_file": get_log_file(),
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            return

        if path == "/api/logs":
            qs = parse_qs(parsed.query)
            try:
                n = int(qs.get("lines", ["300"])[0])
            except ValueError:
                n = 300
            n = max(1, min(n, 2000))
            self._send_json({"lines": tail_log(get_log_file(), n)})
            return

        if path == "/api/config":
            cfg = load_config()
            self._send_json(
                {
                    "mediasage_host": get_mediasage_host(),
                    "mediasage_host_is_custom": bool(cfg.get("mediasage_host")),
                    "mediasage_host_default": DEFAULT_MEDIASAGE_HOST,
                    "log_file": get_log_file(),
                    "log_file_is_custom": bool(cfg.get("log_file")),
                    "log_file_default": DEFAULT_LOG_FILE,
                }
            )
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/config":
            body = self._read_json_body()
            host = str(body.get("mediasage_host", "")).strip()
            if not is_valid_mediasage_host(host):
                self._send_json(
                    {"ok": False, "error": "Enter a valid URL starting with http:// or https://"},
                    status=400,
                )
                return
            log_file = str(body.get("log_file", "")).strip()
            cfg = {"mediasage_host": host}
            if log_file:
                cfg["log_file"] = log_file
            save_config(cfg)
            self._send_json(
                {
                    "ok": True,
                    "mediasage_host": host,
                    "log_file": log_file or DEFAULT_LOG_FILE,
                    "message": "Saved. Restart the listener to apply.",
                }
            )
            return

        if path == "/api/config/test":
            body = self._read_json_body()
            host = str(body.get("mediasage_host", "")).strip() or get_mediasage_host()
            if not is_valid_mediasage_host(host):
                self._send_json(
                    {"ok": False, "error": "Enter a valid URL starting with http:// or https://"},
                    status=400,
                )
                return
            ok, status_code, detail = test_mediasage_connection(host)
            self._send_json({"ok": ok, "status_code": status_code, "detail": detail, "host": host})
            return

        if path == "/api/enable":
            set_enabled(True)
            self._send_json({"ok": True, "enabled": True})
            return

        if path == "/api/disable":
            set_enabled(False)
            self._send_json({"ok": True, "enabled": False})
            return

        if path == "/api/restart":
            ok = restart_listener(LISTENER_PORT)
            self._send_json(
                {
                    "ok": ok,
                    "running": is_port_open(LISTENER_PORT),
                    "message": (
                        "Listener restarted successfully."
                        if ok
                        else "Listener did not come back up in time. Check the console log."
                    ),
                }
            )
            return

        self.send_response(404)
        self.end_headers()


def _write_dashboard_pid_file():
    try:
        os.makedirs(os.path.dirname(DASHBOARD_PID_FILE), exist_ok=True)
        with open(DASHBOARD_PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _remove_dashboard_pid_file():
    try:
        if os.path.exists(DASHBOARD_PID_FILE):
            with open(DASHBOARD_PID_FILE, "r") as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(DASHBOARD_PID_FILE)
    except Exception:
        pass


def _handle_dashboard_termination(signum, frame):
    _remove_dashboard_pid_file()
    sys.exit(0)


def run(port=8001):
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")

    _write_dashboard_pid_file()
    signal.signal(signal.SIGTERM, _handle_dashboard_termination)
    signal.signal(signal.SIGINT, _handle_dashboard_termination)

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Smart Plex Queue Dashboard running at http://{host}:{port}")
    print(f"Controlling listener on port {LISTENER_PORT} (toggle file: {TOGGLE_FILE})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _remove_dashboard_pid_file()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    run(port)
