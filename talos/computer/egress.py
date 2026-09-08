"""Public HTTP(S) egress for a QEMU guest with restrict=on.

No host routes are exposed. DNS is resolved here and the checked numeric address
is used for the connection (including CONNECT), avoiding a DNS rebind window.
"""
from __future__ import annotations
import http.server
import ipaddress
import select
import socket
import socketserver
import time
import urllib.parse

MAX_HEADER = 16384
MAX_SECONDS = 120
PORTS = {80, 443}


def public_addresses(host: str, port: int) -> list[tuple]:
    if port not in PORTS:
        raise ValueError("only public HTTP and HTTPS are available")
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise ValueError("private and special-purpose destinations are blocked")
    return answers


def connect_public(host: str, port: int) -> socket.socket:
    answers = public_addresses(host, port)
    for family, kind, proto, _, address in answers:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(10)
        try:
            sock.connect(address)
            return sock
        except OSError:
            sock.close()
    raise OSError("destination unavailable")


def relay(left: socket.socket, right: socket.socket) -> None:
    until = time.monotonic() + MAX_SECONDS
    while time.monotonic() < until:
        ready, _, _ = select.select([left, right], [], [], 1)
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    # Don't buffer beyond HTTP headers: CONNECT must preserve early TLS bytes.
    rbufsize = 0

    def log_message(self, *_):
        pass  # URLs can contain private searches or tokens.

    def handle_one_request(self):
        self.connection.settimeout(15)
        return super().handle_one_request()

    def do_CONNECT(self):
        established = False
        try:
            parsed = urllib.parse.urlsplit("//" + self.path)
            if parsed.username or parsed.password or parsed.path:
                raise ValueError("invalid CONNECT authority")
            with connect_public(parsed.hostname or "", parsed.port or 443) as remote:
                self.send_response(200, "Connection established")
                self.end_headers()
                established = True
                relay(self.connection, remote)
        except (ValueError, OSError, TimeoutError) as error:
            self.close_connection = True
            # May already be a tunnel; never leak a traceback or upstream data.
            if established:
                return
            self.send_error(403 if isinstance(error, ValueError) else 502, "Destination blocked" if isinstance(error, ValueError) else "Destination unavailable")

    def do_GET(self):
        self._forward()

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET

    def _forward(self):
        response_started = False
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("use HTTP absolute URLs or HTTPS CONNECT")
            if sum(len(k) + len(v) for k, v in self.headers.items()) > MAX_HEADER:
                raise ValueError("headers too large")
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("chunked proxy uploads are not supported")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8 * 1024 * 1024:
                raise ValueError("request too large")
            with connect_public(parsed.hostname, parsed.port or 80) as remote:
                path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                lines = [f"{self.command} {path} HTTP/1.0"]
                excluded = {"proxy-authorization", "proxy-connection", "connection", "host"}
                lines.extend(f"{k}: {v}" for k, v in self.headers.items() if k.lower() not in excluded)
                lines += [f"Host: {parsed.netloc}", "Connection: close", "", ""]
                remote.sendall("\r\n".join(lines).encode("latin-1"))
                remaining = length
                while remaining:
                    data = self.rfile.read(min(65536, remaining))
                    if not data:
                        raise ValueError("incomplete upload")
                    remote.sendall(data)
                    remaining -= len(data)
                while True:
                    data = remote.recv(65536)
                    if not data:
                        break
                    response_started = True
                    self.connection.sendall(data)
        except (ValueError, OSError, TimeoutError) as error:
            if not response_started:
                self.send_error(403 if isinstance(error, ValueError) else 502, "Destination blocked" if isinstance(error, ValueError) else "Destination unavailable")
        self.close_connection = True


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 32


if __name__ == "__main__":
    import os
    endpoint = "/run/talos-computer-egress/proxy.sock"
    if os.path.exists(endpoint):
        os.unlink(endpoint)
    server = Server(endpoint, Proxy)
    os.chmod(endpoint, 0o666)  # Public-only proxy, never a control interface.
    server.serve_forever()
