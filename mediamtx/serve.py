"""Serve this folder (same as `python3 -m http.server`) plus GET /lan-ip,
which returns this machine's LAN IP so the page can show URLs that work
for everyone on the same network.

Usage: python3 serve.py [port]   (default 8080)
"""
import functools
import http.server
import os
import socket
import sys


def lan_ip():
    # Connecting a UDP socket sends nothing; it only makes the OS pick the
    # interface of the default route, whose address is the LAN IP.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/lan-ip":
            return super().do_GET()
        body = lan_ip().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    handler = functools.partial(Handler, directory=os.path.dirname(os.path.abspath(__file__)))
    print(f"Open in a browser: http://{lan_ip()}:{port}")
    http.server.ThreadingHTTPServer(("", port), handler).serve_forever()
