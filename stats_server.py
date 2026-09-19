#!/usr/bin/env python3
"""Read-only psutil stats endpoint for the homelab splash page.

Listens on 127.0.0.1:18790 (tailnet access goes through tailscale serve
which proxies /api/stats to this port). Returns a single JSON object.
"""
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import psutil

HOST = "127.0.0.1"
PORT = 18790


def gather_stats():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu_temp = None
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            if key in temps and temps[key]:
                cpu_temp = temps[key][0].current
                break
        if cpu_temp is None and temps:
            first = next(iter(temps.values()))
            if first:
                cpu_temp = first[0].current
    except Exception:
        cpu_temp = None

    return {
        "hostname": socket.gethostname(),
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "cpu_count": psutil.cpu_count(),
        "ram_used_gb": round(mem.used / 1024**3, 2),
        "ram_total_gb": round(mem.total / 1024**3, 2),
        "ram_percent": mem.percent,
        "disk_used_gb": round(disk.used / 1024**3, 1),
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "disk_percent": disk.percent,
        "uptime_s": int(time.time() - psutil.boot_time()),
        "cpu_temp_c": cpu_temp,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/api/stats"):
            self.send_error(404)
            return
        try:
            payload = json.dumps(gather_stats()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # never leak a stack trace to the page
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # keep the journal quiet; requests are trivial


if __name__ == "__main__":
    HTTPServer((HOST, PORT), Handler).serve_forever()
