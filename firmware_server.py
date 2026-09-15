#!/usr/bin/env python3
"""
ESP32 OTA Firmware Server with argparse support.
Now supports:
    --port <number>
    --host <ip or hostname>

Usage:
    python firmware_server.py
    python firmware_server.py 9000
    python firmware_server.py --port 9000
    python firmware_server.py --host 0.0.0.0 --port 9000
"""

import http.server
import socketserver
import socket
import os
import json
import sys
import argparse
import threading
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import psutil
from zeroconf import Zeroconf, ServiceInfo

# Configuration
DEFAULT_PORT = 8000
DEFAULT_HOST = "0.0.0.0"
BUILDS_DIR = Path(__file__).parent
OTA_MDNS_HOSTNAME = "r33-ota.local."
OTA_MDNS_SERVICE_NAME = "R33 OTA._http._tcp.local."
WIFI_INTERFACE_PATTERNS = ("wi-fi", "wifi", "wireless", "wlan", "802.11")


def get_local_ip():
    """Prefer an active Wi-Fi IPv4 address, then fall back to the default route."""
    addresses = get_local_ips()
    return addresses[0] if addresses else "127.0.0.1"


def get_wifi_ips():
    """Return active, usable Wi-Fi IPv4 addresses in interface order."""
    interface_addresses = psutil.net_if_addrs()
    interface_status = psutil.net_if_stats()
    wifi_ips = []

    for interface_name, addresses in interface_addresses.items():
        if not any(pattern in interface_name.lower() for pattern in WIFI_INTERFACE_PATTERNS):
            continue
        status = interface_status.get(interface_name)
        if not status or not status.isup:
            continue
        for address in addresses:
            if (
                address.family == socket.AF_INET
                and not address.address.startswith("127.")
                and not address.address.startswith("169.254.")
                and address.address not in wifi_ips
            ):
                wifi_ips.append(address.address)

    return wifi_ips


def get_local_ips():
    """Return active, usable IPv4 addresses for all local network interfaces."""
    interface_addresses = psutil.net_if_addrs()
    interface_status = psutil.net_if_stats()
    addresses = []

    for interface_name, interface_addresses_for_name in interface_addresses.items():
        is_wifi = any(pattern in interface_name.lower() for pattern in WIFI_INTERFACE_PATTERNS)
        is_active = interface_status.get(interface_name)

        if not is_active or not is_active.isup:
            continue

        for address in interface_addresses_for_name:
            if (
                address.family == socket.AF_INET
                and not address.address.startswith("127.")
                and not address.address.startswith("169.254.")
            ):
                if address.address not in addresses:
                    addresses.append(address.address)

    wifi_addresses = [
        address.address for interface_name, interface_addresses in psutil.net_if_addrs().items()
        if any(pattern in interface_name.lower() for pattern in WIFI_INTERFACE_PATTERNS)
        for address in interface_addresses
        if address.family == socket.AF_INET and address.address in addresses
    ]
    return wifi_addresses + [address for address in addresses if address not in wifi_addresses]


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class FirmwareRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom request handler for firmware server"""

    def do_GET(self):
        """Handle GET requests"""
        # Strip query string so path comparisons/file serving aren't affected by ?id=...
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        device_id = query.get('id', [None])[0]
        version = query.get('version', [None])[0]
        health_timeout_ms = query.get('health_timeout_ms', [None])[0]

        if path == '/__shutdown':
            shutdown_token = os.environ.get("OTA_SHUTDOWN_TOKEN")
            if shutdown_token and query.get("token", [None])[0] == shutdown_token:
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)
            return

        # Log the request, including the caller's device id when it identifies itself
        if device_id:
            log_parts = [f"[{self.client_address[0]}] GET {path}", f"id={device_id}"]
            if path == '/health' and version:
                log_parts.append(f"version={version}")
            if path == '/health' and health_timeout_ms:
                log_parts.append(f"health_timeout_ms={health_timeout_ms}")
            print(" ".join(log_parts))
        else:
            print(f"[{self.client_address[0]}] GET {path}")

        # Handle metadata.json specially
        if path == '/metadata.json':
            self.serve_metadata()
            return

        # Health check used by the ESP32 to show server status
        if path == '/health':
            self.serve_health(device_id, version, health_timeout_ms)
            return
        
        # Serve files from builds directory
        self.path = path
        self.directory = str(BUILDS_DIR)
        super().do_GET()

    def serve_health(self, device_id=None, version=None, health_timeout_ms=None):
        """Report that the OTA firmware server is running"""
        health = {
            "status": "ok",
            "service": "r33-ota",
            "port": self.server.server_port,
            "device_id": device_id or "unknown",
        }
        if version:
            health["current_version"] = version
        if health_timeout_ms:
            try:
                health["health_timeout_ms"] = max(1, int(health_timeout_ms))
            except ValueError:
                pass
        payload = json.dumps(health).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(payload))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(payload)

    def serve_metadata(self):
        """Serve firmware metadata from metadata.json file"""
        metadata_file = BUILDS_DIR / "metadata.json"

        if metadata_file.exists():
            try:
                with open(metadata_file, 'r') as f:
                    metadata = json.load(f)

                if "versions" in metadata:
                    for device in metadata["versions"]:
                        if "url" in metadata["versions"][device]:
                            # Advertise a stable hostname while IP URLs remain valid input.
                            url = metadata["versions"][device]["url"]
                            url = url.replace("YOUR_LAPTOP_IP", OTA_MDNS_HOSTNAME.rstrip("."))
                            url = url.replace("8000", str(self.server.server_port))
                            metadata["versions"][device]["url"] = url

                        # Update file size with actual binary size
                        file_size = self.get_file_size(f"{device}/r33-{device}.bin")
                        if file_size > 0:
                            metadata["versions"][device]["size"] = file_size
                            print(f"  -> {device}: {file_size} bytes")

                # Send response
                response = json.dumps(metadata, indent=2).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', len(response))
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(response)
                print(f"  -> Served metadata.json ({len(response)} bytes)")
                print(f"  -> Latest version: {metadata.get('latest_version', 'unknown')}")
                return

            except Exception as e:
                print(f"  -> Error reading metadata.json: {e}")

        # Fallback if metadata.json doesn't exist
        print("  -> metadata.json not found, using defaults")
        metadata = {
            "latest_version": "1.0.1",
            "versions": {
                "r33-master": {
                    "latest": "1.0.1",
                    "url": f"http://{OTA_MDNS_HOSTNAME.rstrip('.')}:{self.server.server_port}/master/r33-master.bin",
                    "size": self.get_file_size("master/r33-master.bin")
                },
                "r33-slave": {
                    "latest": "1.0.1",
                    "url": f"http://{OTA_MDNS_HOSTNAME.rstrip('.')}:{self.server.server_port}/slave/r33-slave.bin",
                    "size": self.get_file_size("slave/r33-slave.bin")
                }
            }
        }

        response = json.dumps(metadata, indent=2).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(response))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(response)

    def get_file_size(self, relative_path):
        """Get file size or return 0 if not found"""
        file_path = BUILDS_DIR / relative_path
        if file_path.exists():
            return file_path.stat().st_size
        return 0

    def log_message(self, format, *args):
        """Override to customize logging"""
        # Suppress default logging - we do our own in do_GET
        pass


def parse_args():
    parser = argparse.ArgumentParser(description="ESP32 OTA Firmware Server")

    parser.add_argument(
        "positional_port",
        nargs="?",
        type=int,
        default=None,
        help="Port number (positional, optional)"
    )

    parser.add_argument(
        "--port",
        dest="option_port",
        type=int,
        help="Port number (named)"
    )

    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help="Host/IP to bind (default: 0.0.0.0)"
    )

    args = parser.parse_args()

    # Resolve port priority: --port > positional > default
    port = args.option_port or args.positional_port or DEFAULT_PORT

    return args.host, port


def main():
    """Start the firmware server"""
    host, port = parse_args()

    if not BUILDS_DIR.exists():
        print(f"Error: Builds directory not found at {BUILDS_DIR}")
        sys.exit(1)

    local_ips = get_local_ips()
    wifi_ips = get_wifi_ips()
    local_ip = wifi_ips[0] if wifi_ips else (local_ips[0] if local_ips else "127.0.0.1")
    print(f"Starting ESP32 OTA Firmware Server", flush=True)
    print(f"WIFI network needs to be designated as private on this laptop")
    print(f"If not private, mdns/GET xxx may not work correctly")
    print(f"Serving files from: {BUILDS_DIR}")
    print(f"Listening on IP {local_ip} port {port}... Host: {host}")
    print()
    print("Endpoints:")
    print(f"  GET /metadata.json")
    print(f"  GET /health")
    print(f"  GET /master/r33-master.bin")
    print(f"  GET /slave/r33-slave.bin")
    print()
    print("Configuration URLs:")
    print(f"  http://{local_ip}:{port}")
    print(f"  http://{OTA_MDNS_HOSTNAME.rstrip('.')}:{port}")
    print()
    print("Press Ctrl+C to stop the server")
    print()

    try:
        with ReusableTCPServer((host, port), FirmwareRequestHandler) as httpd:
            httpd.server_name = OTA_MDNS_HOSTNAME.rstrip(".")
            httpd.server_port = port
            if host in ("", "0.0.0.0"):
                advertised_ip = wifi_ips[0] if wifi_ips else local_ip
            else:
                advertised_ip = host
            advertised_ips = [advertised_ip]

            service_info = ServiceInfo(
                "_http._tcp.local.",
                OTA_MDNS_SERVICE_NAME,
                addresses=[socket.inet_aton(address) for address in advertised_ips],
                port=port,
                properties={"path": "/metadata.json"},
                server=OTA_MDNS_HOSTNAME,
            )

            # Bind mDNS multicast to the same Wi-Fi interface used by the
            # service record instead of allowing Windows to choose an adapter.
            mdns_state = {
                "zeroconf": Zeroconf(interfaces=[advertised_ip]),
                "service_info": service_info,
                "advertised_ip": advertised_ip,
            }
            mdns_state["zeroconf"].register_service(service_info)
            print(
                f"Advertising mDNS service at http://{OTA_MDNS_HOSTNAME.rstrip('.')}:{port} "
                f"({advertised_ip}) via {advertised_ip}"
            )

            announce_stop = threading.Event()

            def refresh_mdns():
                # DHCP renewal, Wi-Fi power-saving, or adapter sleep can change/drop the
                # bound interface, silently killing multicast without the process crashing.
                # Re-detect the IP and fully re-create Zeroconf when that happens.
                while not announce_stop.wait(15):
                    try:
                        current_wifi_ips = get_wifi_ips()
                        current_ip = current_wifi_ips[0] if current_wifi_ips else None
                        if host not in ("", "0.0.0.0"):
                            current_ip = host

                        if current_ip and current_ip != mdns_state["advertised_ip"]:
                            print(
                                f"Detected IP change ({mdns_state['advertised_ip']} -> {current_ip}); "
                                f"re-advertising mDNS service.",
                                flush=True,
                            )
                            try:
                                mdns_state["zeroconf"].unregister_service(mdns_state["service_info"])
                            except Exception:
                                pass
                            mdns_state["zeroconf"].close()

                            new_service_info = ServiceInfo(
                                "_http._tcp.local.",
                                OTA_MDNS_SERVICE_NAME,
                                addresses=[socket.inet_aton(current_ip)],
                                port=port,
                                properties={"path": "/metadata.json"},
                                server=OTA_MDNS_HOSTNAME,
                            )
                            new_zeroconf = Zeroconf(interfaces=[current_ip])
                            new_zeroconf.register_service(new_service_info)

                            mdns_state["zeroconf"] = new_zeroconf
                            mdns_state["service_info"] = new_service_info
                            mdns_state["advertised_ip"] = current_ip
                        else:
                            mdns_state["zeroconf"].update_service(mdns_state["service_info"])
                    except Exception as exc:
                        print(f"mDNS refresh failed: {exc}", flush=True)

            announce_thread = threading.Thread(target=refresh_mdns, daemon=True)
            announce_thread.start()

            try:
                httpd.serve_forever()
            finally:
                announce_stop.set()
                announce_thread.join(timeout=2)
                try:
                    mdns_state["zeroconf"].unregister_service(mdns_state["service_info"])
                finally:
                    mdns_state["zeroconf"].close()

    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)

    except OSError as e:
        print(f"Error: {e}")
        if e.errno == 48 or e.errno == 98:  # Port already in use
            print(f"Port {port} is already in use. Try a different port:")
            print(f"  python firmware_server.py {port + 1}")
        sys.exit(1)


if __name__ == '__main__':
    main()