from PyQt6.QtCore import QObject, pyqtSignal
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime
from urllib.parse import quote
from urllib.request import urlopen

# Matches the access-log line printed by firmware_server.py: "[1.2.3.4] GET /master/r33-master.bin"
# and optionally health fields sent by the device as query parameters.
_REQUEST_RE = re.compile(
    r"^\[(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\]\s+(?:GET|POST|PUT|DELETE)\s+(?P<path>\S+)"
    r"(?:\s+id=(?P<devid>\S+))?"
    r"(?:\s+version=(?P<version>\S+))?"
    r"(?:\s+health_timeout_ms=(?P<health_timeout_ms>\d+))?"
)


class ServerController(QObject):
    # Signals expected by UI panels
    status_changed   = pyqtSignal(str)   # "starting", "running", "stopped", "error"
    stats_updated    = pyqtSignal(dict)  # uptime, requests, bytes_served, device_count
    device_connected = pyqtSignal(dict)  # full device dict
    device_updated   = pyqtSignal(dict)  # full device dict
    device_removed   = pyqtSignal(str)   # device_id, when merged into a better-known id
    log_message      = pyqtSignal(str)   # log lines for LogPanel

    def __init__(self, log_callback):
        super().__init__()
        self.process = None
        self.log_callback = log_callback
        self.log_thread = None
        self.running = False

        # Device tracking
        self.devices = {}
        self._ip_to_id = {}  # remembers the real device id once an ip announces one via ?id=
        self._start_time = None
        self._stopping = False
        self._run_id = 0
        self._shutdown_token = ""

        # Config storage (required by ui_mainwindow.py)
        self.python_exe = ""
        self.server_script = ""
        self.server_host = ""
        self.server_port = ""

        # GUI closeEvent expects this
        self.status = "stopped"

    # ----------------------------------------------------------------------
    # START SERVER
    # ----------------------------------------------------------------------
    def start(self, python_exe, script_path, host, port):
        if self.running:
            self.log_message.emit("[WARN] Server already running.")
            return

        # Store config
        self.python_exe = python_exe
        self.server_script = script_path
        self.server_host = host
        self.server_port = port

        self.status = "starting"
        self.status_changed.emit("starting")

        cmd = [
            python_exe,
            "-u",  # unbuffered stdout so log lines appear live, not just at exit
            script_path,
            "--host", host,
            "--port", str(port)
        ]

        # Force child's stdout unbuffered even if -u is ignored by the interpreter used
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self._shutdown_token = secrets.token_urlsafe(24)
        env["OTA_SHUTDOWN_TOKEN"] = self._shutdown_token

        # Launch server FIRST
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # <-- merge stderr into stdout
                text=True,
                bufsize=1,
                env=env
            )
        except Exception as e:
            msg = f"[ERROR] Failed to start server: {e}"
            self.log_message.emit(msg)
            self.status = "error"
            self.status_changed.emit("error")
            return

        # Now safe to start log thread
        self._run_id += 1
        run_id = self._run_id
        self.running = True
        self._start_time = time.time()

        self.log_thread = threading.Thread(
            target=self._stream_logs,
            args=(self.process, run_id),
            daemon=True
        )
        self.log_thread.start()

        self.status = "running"
        self.status_changed.emit("running")

        # Stats loop
        threading.Thread(
            target=self._update_stats_loop,
            daemon=True
        ).start()

    # ----------------------------------------------------------------------
    # DEVICE REGISTRATION
    # ----------------------------------------------------------------------
    def _register_device(self, device_id, device_type="unknown", device_ip=""):
        now = datetime.now().strftime("%H:%M:%S")

        dev = {
            "id": device_id,
            "ip": device_ip,
            "type": device_type,
            "first_seen": now,
            "last_seen": now,
            "active_since": now,
            "requests": 0,
            "status": "active",
            "current_version": "Unknown",
            "health_timeout_ms": 60000,
            "last_event": "",
            "last_log": "",
        }

        self.devices[device_id] = dev
        self.device_connected.emit(dev)

    @staticmethod
    def _device_type_from_path(path):
        if path.startswith("/master/"):
            return "master"
        if path.startswith("/slave/"):
            return "slave"
        return None

    def _merge_device(self, old_id, new_id):
        """Fold a temporary ip-keyed device entry into the id the device later announced."""
        old = self.devices.pop(old_id, None)
        if not old:
            return

        if new_id in self.devices:
            target = self.devices[new_id]
            target["requests"] += old["requests"]
            target["first_seen"] = min(target["first_seen"], old["first_seen"])
            self.device_updated.emit(target)
        else:
            old["id"] = new_id
            self.devices[new_id] = old
            self.device_connected.emit(old)

        self.device_removed.emit(old_id)

    # ----------------------------------------------------------------------
    # LOG STREAMING
    # ----------------------------------------------------------------------
    def _stream_logs(self, process, run_id):
        try:
            for line in process.stdout:
                if line.strip():
                    clean = line.rstrip()

                    # Send to log panel
                    self.log_message.emit(clean)

                    # Only track real requests ("[ip] GET /path"), not generic server log lines
                    match = _REQUEST_RE.match(clean)
                    if match:
                        ip = match.group("ip")
                        devid = match.group("devid")
                        version = match.group("version")
                        timeout_text = match.group("health_timeout_ms")
                        device_type = self._device_type_from_path(match.group("path"))

                        if devid:
                            # This ip just told us its real id - fold any earlier ip-keyed entry into it
                            previous_id = self._ip_to_id.get(ip)
                            self._ip_to_id[ip] = devid
                            if previous_id and previous_id != devid and previous_id in self.devices:
                                self._merge_device(previous_id, devid)
                            device_id = devid
                        else:
                            # Anonymous health probes verify the server but are not device registrations.
                            if match.group("path") == "/health" and ip not in self._ip_to_id:
                                continue
                            # No id on this request - use whatever id this ip has already announced
                            device_id = self._ip_to_id.get(ip, ip)

                        health_timeout_ms = int(timeout_text) if timeout_text else None
                        self._update_device_log(
                            device_id, clean, device_type, ip, version, health_timeout_ms
                        )

        except Exception as e:
            msg = f"[ERROR] Log thread error: {e}"
            self.log_message.emit(msg)

        process.wait()
        exit_code = process.returncode

        msg = f"[INFO] Server process exited (code {exit_code})."
        self.log_message.emit(msg)

        if process is not self.process or run_id != self._run_id:
            return

        self.running = False

        if self._stopping or exit_code == 0:
            self.status = "stopped"
            if not self._stopping:
                self.status_changed.emit("stopped")
        else:
            self.status = "error"
            self.status_changed.emit("error")

        self._stopping = False

    # ----------------------------------------------------------------------
    # DEVICE LOG UPDATE
    # ----------------------------------------------------------------------
    def _update_device_log(
        self, device_id, log_line, device_type=None, device_ip="",
        version=None, health_timeout_ms=None
    ):
        if device_id not in self.devices:
            self._register_device(device_id, device_type or "unknown", device_ip)

        dev = self.devices[device_id]
        if device_ip:
            dev["ip"] = device_ip
        if device_type:
            dev["type"] = device_type
        if version:
            dev["current_version"] = version
        if health_timeout_ms:
            dev["health_timeout_ms"] = max(1, health_timeout_ms)
        if dev.get("status") == "offline":
            dev["active_since"] = datetime.now().strftime("%H:%M:%S")
        dev["status"] = "active"
        dev["last_log"] = log_line
        dev["last_event"] = log_line
        dev["last_seen"] = datetime.now().strftime("%H:%M:%S")
        dev["requests"] += 1

        self.device_updated.emit(dev)

    # ----------------------------------------------------------------------
    # STATS LOOP
    # ----------------------------------------------------------------------
    def _update_stats_loop(self):
        while self.running:
            uptime = time.time() - self._start_time

            stats = {
                "uptime": time.strftime("%H:%M:%S", time.gmtime(uptime)),
                "requests": sum(d["requests"] for d in self.devices.values()),
                "bytes_served": 0,
                "device_count": len(self.devices),
            }

            self.stats_updated.emit(stats)
            time.sleep(1)

    # ----------------------------------------------------------------------
    # STOP SERVER
    # ----------------------------------------------------------------------
    def stop(self):
        if not self.running or not self.process:
            self.log_message.emit("[INFO] Server is not running.")
            return

        self.log_message.emit("[INFO] Stopping server...")

        self._stopping = True
        self.status = "stopping"
        self.status_changed.emit("stopping")

        process = self.process
        try:
            shutdown_host = "127.0.0.1" if self.server_host in ("", "0.0.0.0") else self.server_host
            shutdown_url = (
                f"http://{shutdown_host}:{self.server_port}/__shutdown?token="
                f"{quote(self._shutdown_token)}"
            )
            shutdown_sent = False
            shutdown_deadline = time.time() + 2.5
            while time.time() < shutdown_deadline and process.poll() is None:
                try:
                    with urlopen(shutdown_url, timeout=0.25):
                        shutdown_sent = True
                    break
                except Exception:
                    time.sleep(0.1)
            if not shutdown_sent and process.poll() is None:
                self.log_message.emit("[WARN] Graceful shutdown request failed; forcing shutdown.")
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.log_message.emit("[WARN] Server did not stop cleanly; forcing shutdown.")
                process.terminate()
                process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception as e:
                msg = f"[ERROR] Failed to kill server: {e}"
                self.log_message.emit(msg)

        if self.log_thread and self.log_thread is not threading.current_thread():
            self.log_thread.join(timeout=1)
        self.running = False
        self.status = "stopped"
        self.status_changed.emit("stopped")
        self._stopping = False
        self.log_message.emit("[INFO] Server stopped.")

    # ----------------------------------------------------------------------
    # RESTART SERVER
    # ----------------------------------------------------------------------
    def restart(self, python_exe, script_path, host, port):
        self.stop()
        self.start(python_exe, script_path, host, port)