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
import asyncio
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from aiohttp import web, WSMsgType
import psutil
from zeroconf import Zeroconf, ServiceInfo
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
from zeroconf import ServiceStateChange

# Configuration
DEFAULT_PORT = 8000
DEFAULT_HOST = "0.0.0.0"
SCRIPT_DIR = Path(__file__).parent  # metadata.json always lives alongside this script
BUILDS_DIR = Path(__file__).parent
OTA_MDNS_HOSTNAME = "r33-ota.local."
OTA_MDNS_SERVICE_NAME = "R33 OTA._http._tcp.local."
WIFI_INTERFACE_PATTERNS = ("wi-fi", "wifi", "wireless", "wlan", "802.11")


def log_request(request):
    device_id = request.query.get("id")
    if device_id:
        log_parts = [f"[{request.remote}] {request.method} {request.path}", f"id={device_id}"]
        if request.path == "/health" and request.query.get("version"):
            log_parts.append(f"version={request.query['version']}")
        if request.path == "/health" and request.query.get("health_timeout_ms"):
            log_parts.append(f"health_timeout_ms={request.query['health_timeout_ms']}")
        print(" ".join(log_parts), flush=True)
    else:
        print(f"[{request.remote}] {request.method} {request.path}", flush=True)


def build_metadata(port):
    metadata_file = SCRIPT_DIR / "metadata.json"
    if metadata_file.exists():
        try:
            with open(metadata_file, "r", encoding="utf-8") as file:
                metadata = json.load(file)
            for device, details in metadata.get("versions", {}).items():
                if "url" in details:
                    details["url"] = details["url"].replace(
                        "YOUR_LAPTOP_IP", OTA_MDNS_HOSTNAME.rstrip(".")
                    ).replace("8000", str(port))
                firmware_file = BUILDS_DIR / device / f"r33-{device}.bin"
                if firmware_file.exists():
                    details["size"] = firmware_file.stat().st_size
            return metadata
        except Exception as exc:
            print(f"  -> Error reading metadata.json: {exc}", flush=True)

    return {
        "latest_version": "1.0.1",
        "versions": {
            "r33-master": {
                "latest": "1.0.1",
                "url": f"http://{OTA_MDNS_HOSTNAME.rstrip('.')}:{port}/master/r33-master.bin",
            },
            "r33-slave": {
                "latest": "1.0.1",
                "url": f"http://{OTA_MDNS_HOSTNAME.rstrip('.')}:{port}/slave/r33-slave.bin",
            },
        },
    }


async def metadata_handler(request):
    log_request(request)
    return web.json_response(build_metadata(request.app["port"]), headers={"Cache-Control": "no-cache"})


async def health_handler(request):
    log_request(request)
    health = {
        "status": "ok",
        "service": "r33-ota",
        "port": request.app["port"],
        "device_id": request.query.get("id", "unknown"),
    }
    if request.query.get("version"):
        health["current_version"] = request.query["version"]
    if request.query.get("health_timeout_ms"):
        try:
            health["health_timeout_ms"] = max(1, int(request.query["health_timeout_ms"]))
        except ValueError:
            pass
    return web.json_response(health, headers={"Cache-Control": "no-cache"})


async def firmware_handler(request):
    log_request(request)
    build_root = BUILDS_DIR.absolute()
    firmware_path = (build_root / request.match_info["filename"]).absolute()
    try:
        firmware_path.relative_to(build_root)
    except ValueError:
        raise web.HTTPForbidden()
    if not firmware_path.is_file():
        raise web.HTTPNotFound()

    response = web.StreamResponse(
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(firmware_path.stat().st_size),
        }
    )
    await response.prepare(request)
    device_id = request.query.get("id")
    if not device_id:
        device_id = request.app["device_ips"].get(request.remote)
    total_size = firmware_path.stat().st_size
    transferred = 0
    last_logged_percent = -1
    with open(firmware_path, "rb") as firmware_file:
        while chunk := firmware_file.read(8192):
            await response.write(chunk)
            transferred += len(chunk)
            if device_id:
                percentage = min(100, int(transferred * 100 / total_size))
                if percentage - last_logged_percent >= 10 or percentage == 100:
                    print(
                        f"FIRMWARE_PROGRESS id={device_id} percent={percentage}",
                        flush=True,
                    )
                    last_logged_percent = percentage
    await response.write_eof()
    print(f"FIRMWARE_SERVED bytes={transferred}", flush=True)
    return response


async def shutdown_handler(request):
    if request.query.get("token") != os.environ.get("OTA_SHUTDOWN_TOKEN"):
        raise web.HTTPNotFound()
    request.app["shutdown_event"].set()
    return web.Response()


async def device_websocket_handler(request):
    device_id = request.query.get("id")
    if not device_id:
        raise web.HTTPBadRequest(text="Missing device id")

    websocket = web.WebSocketResponse(heartbeat=20)
    await websocket.prepare(request)
    request.app["devices"][device_id] = websocket
    request.app["device_ips"][request.remote] = device_id
    print(f"[{request.remote}] GET /device id={device_id}", flush=True)
    await websocket.send_json({"status": "registered", "device_id": device_id})

    try:
        async for message in websocket:
            if message.type == WSMsgType.TEXT:
                try:
                    status = json.loads(message.data)
                    print(
                        f"[{request.remote}] DEVICE_STATUS id={device_id} "
                        f"command_id={status.get('id', '')} status={status.get('status', '')}",
                        flush=True,
                    )
                except ValueError:
                    pass
    finally:
        if request.app["devices"].get(device_id) is websocket:
            request.app["devices"].pop(device_id, None)
        # Keep the IP mapping while the device reboots. Its firmware download
        # can begin before the device opens its replacement websocket.
        print(f"[{request.remote}] DEVICE_DISCONNECTED id={device_id}", flush=True)
    return websocket


async def command_handler(request):
    device_id = request.match_info["device_id"]
    try:
        command = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    if command.get("command") != "start_update":
        raise web.HTTPBadRequest(text="Unsupported command")

    websocket = request.app["devices"].get(device_id)
    if websocket is None or websocket.closed:
        raise web.HTTPNotFound(text="Device is not connected")

    command_id = str(uuid.uuid4())
    await websocket.send_json({"id": command_id, "command": "start_update"})
    print(f"[SERVER] COMMAND id={device_id} command_id={command_id} start_update", flush=True)
    return web.json_response({"status": "Command sent", "id": command_id}, status=202)


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


def get_interface_name(ip):
    """Return the network interface name bound to the given IPv4 address."""
    for interface_name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family == socket.AF_INET and address.address == ip:
                return interface_name
    return "unknown"


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
        metadata_file = SCRIPT_DIR / "metadata.json"

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

    parser.add_argument(
        "--builds-dir",
        type=str,
        default=None,
        help="Directory to serve firmware builds from (default: script directory)"
    )

    args = parser.parse_args()

    # Resolve port priority: --port > positional > default
    port = args.option_port or args.positional_port or DEFAULT_PORT

    return args.host, port, args.builds_dir


async def run_server(host, port, advertised_ips):
    app = web.Application()
    app["port"] = port
    app["devices"] = {}
    app["device_ips"] = {}
    app["shutdown_event"] = asyncio.Event()
    app.router.add_get("/metadata.json", metadata_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/device", device_websocket_handler)
    app.router.add_post("/commands/{device_id}", command_handler)
    app.router.add_get("/__shutdown", shutdown_handler)
    app.router.add_get("/{filename:.*}", firmware_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    service_info = ServiceInfo(
        "_http._tcp.local.", OTA_MDNS_SERVICE_NAME,
        addresses=[socket.inet_aton(ip) for ip in advertised_ips], port=port,
        properties={"path": "/metadata.json"}, server=OTA_MDNS_HOSTNAME,
    )
    # Registering with all advertised interfaces lets mDNS respond on Ethernet and Wi-Fi at once.
    zeroconf = AsyncZeroconf(interfaces=advertised_ips)
    await zeroconf.async_register_service(service_info)
    mdns_instance = zeroconf

    mdns_browsers = []
    discovered_service_types = set()
    responder_hostnames = {}

    def on_responder_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            asyncio.create_task(resolve_responder(service_type, name))
        elif state_change is ServiceStateChange.Removed:
            responder_key = (service_type, name)
            hostname = responder_hostnames.pop(responder_key, None)
            if hostname:
                print(f"MDNS_RESPONDER state=removed name={hostname}", flush=True)

    async def resolve_responder(service_type, name):
        service = await mdns_instance.async_get_service_info(service_type, name)
        if service is None or not service.server:
            return
        hostname = service.server.rstrip(".")
        responder_hostnames[(service_type, name)] = hostname
        print(f"MDNS_RESPONDER state=added name={hostname}", flush=True)

    def on_service_type_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added and name not in discovered_service_types:
            discovered_service_types.add(name)
            mdns_browsers.append(
                AsyncServiceBrowser(
                    mdns_instance.zeroconf, name, handlers=[on_responder_change]
                )
            )

    mdns_browsers.append(
        AsyncServiceBrowser(
            mdns_instance.zeroconf,
            "_services._dns-sd._udp.local.",
            handlers=[on_service_type_change],
        )
    )
    for advertised_ip in advertised_ips:
        interface_name = get_interface_name(advertised_ip)
        print(
            f"Advertising mDNS service at http://{OTA_MDNS_HOSTNAME.rstrip('.')}:{port} "
            f"({advertised_ip}) via {advertised_ip} on interface {interface_name}", flush=True,
        )
        print(
            f"MDNS_INFO interface={interface_name} ip={advertised_ip} "
            f"host={OTA_MDNS_HOSTNAME.rstrip('.')}", flush=True,
        )

    try:
        await app["shutdown_event"].wait()
    finally:
        try:
            for browser in mdns_browsers:
                await browser.async_cancel()
            await zeroconf.async_unregister_service(service_info)
        finally:
            await zeroconf.async_close()
        await runner.cleanup()


def main():
    """Start the firmware server"""
    global BUILDS_DIR
    host, port, builds_dir = parse_args()
    if builds_dir:
        BUILDS_DIR = Path(builds_dir)

    if not BUILDS_DIR.exists():
        print(f"Error: Builds directory not found at {BUILDS_DIR}")
        sys.exit(1)

    local_ips = get_local_ips()
    if host in ("", "0.0.0.0"):
        # Listening on all interfaces, so report every active local address (LAN + Wi-Fi).
        listen_ips = local_ips or ["127.0.0.1"]
    else:
        listen_ips = [host]
    print(f"Starting ESP32 OTA Firmware Server", flush=True)
    print(f"WIFI network needs to be designated as private on this laptop")
    print(f"If not private, mdns/GET xxx may not work correctly")
    print(f"Serving files from: {BUILDS_DIR}")
    for ip in listen_ips:
        print(f"Listening on IP {ip} port {port}... Host: {host}")
    print()
    print("Endpoints:")
    print(f"  GET /metadata.json")
    print(f"  GET /health")
    print(f"  GET /master/r33-master.bin")
    print(f"  GET /slave/r33-slave.bin")
    print()
    print("Configuration URLs:")
    for ip in listen_ips:
        print(f"  http://{ip}:{port}")
    print(f"  http://{OTA_MDNS_HOSTNAME.rstrip('.')}:{port}")
    print()
    print("Press Ctrl+C to stop the server")
    print()

    if host in ("", "0.0.0.0"):
        # Advertise on every active local interface (e.g. Ethernet + Wi-Fi) at once.
        advertised_ips = local_ips or ["127.0.0.1"]
    else:
        advertised_ips = [host]

    try:
        asyncio.run(run_server(host, port, advertised_ips))
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