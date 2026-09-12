"""Authenticated workbench beside the agent, with no approval endpoint."""
import base64
import hashlib
import hmac
import json
from http.cookies import SimpleCookie
import mimetypes
from pathlib import Path
import secrets
import socket
import sqlite3
import time
from urllib.parse import urlsplit, parse_qs

try:
    from websockify import auth_plugins, websocketproxy

    _ProxyBase = websocketproxy.ProxyRequestHandler
except ImportError:  # pragma: no cover - siehe unten
    # ⚠️ `websockify` liegt auf dem COMPUTER-Host (apt, `deploy/computer-setup.py`),
    # nicht im venv des Agenten. Bis zum 12.09. stand der Import hart am Modulkopf,
    # und `tests/test_computer_web.py` uebersprang sich deshalb auf jeder Maschine
    # ohne diese Bibliothek — auch in der CI. Uebersprungen waren ausgerechnet die
    # Faelle, die die Sitzungssignatur und die Login-Bremse pruefen: zwei
    # Sicherheitseigenschaften, die damit nirgends belegt waren.
    #
    # Nur der PROXY braucht die Bibliothek. Signatur, Cookie-Pruefung und Rate-Limit
    # sind reines Python und gehoeren geprueft, wo immer die Tests laufen. Der
    # Platzhalter macht das Modul importierbar; wer den Server wirklich startet,
    # bekommt in `main()` eine klare Ansage statt eines AttributeError.
    auth_plugins = websocketproxy = None

    class _ProxyBase:
        pass


from .contract import slug

CONFIG = {}
COOKIE = "talos_computer"
ASSETS = Path(__file__).with_name("web")
NOVNC = Path("/usr/share/novnc")
CAPTURES = Path("/var/lib/talos-computer-captures")


def rpc(kind, args):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(15)
        sock.connect("/run/talos-computer-api/control.sock")
        sock.sendall(json.dumps({"kind": kind, "args": args}).encode() + b"\n")
        raw = sock.makefile("rb").readline(1000000)
    result = json.loads(raw)
    if "error" in result:
        raise ValueError(result["error"])
    return result


def make_cookie(secret, now=None):
    expires = int(now or time.time()) + 8 * 3600
    payload = f"{expires}.{secrets.token_hex(16)}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def valid_cookie(header, secret, now=None):
    try:
        parsed = SimpleCookie()
        parsed.load(header or "")
        value = parsed[COOKIE].value
        expires, nonce, signature = value.split(".")
        payload = expires + "." + nonce
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        current = now or time.time()
        return (len(nonce) == 32 and current < int(expires) <= current + 8 * 3600
                and hmac.compare_digest(expected, signature))
    except (KeyError, ValueError):
        return False


def login_allowed(path, now):
    # Websockify forks connections, so a Python list would reset per request.
    with sqlite3.connect(str(path), timeout=5) as db:
        db.execute("CREATE TABLE IF NOT EXISTS attempts (ts REAL NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM attempts WHERE ts < ?", (now - 60,))
        if db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] >= 8:
            return False
        db.execute("INSERT INTO attempts VALUES (?)", (now,))
        return True


class Auth:
    def authenticate(self, headers, target_host, target_port):
        if not valid_cookie(headers.get("Cookie"), CONFIG["view_secret"]):
            raise auth_plugins.AuthenticationError(response_code=401)
        if headers.get("Upgrade", "").lower() == "websocket":
            if headers.get("Origin") != CONFIG["origin"]:
                raise auth_plugins.AuthenticationError(response_code=403)
            if rpc("read", {"op": "status"})["control"] != "human":
                raise auth_plugins.AuthenticationError(response_code=409)


class Handler(_ProxyBase):
    def log_message(self, *_):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        super().end_headers()

    def respond(self, code, body, kind="application/json; charset=utf-8", cookie=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        return valid_cookie(self.headers.get("Cookie"), CONFIG["view_secret"])

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in {"/", "/app.js", "/style.css"}:
            name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
            return self.respond(200, (ASSETS / name).read_bytes(),
                                mimetypes.guess_type(name)[0] + "; charset=utf-8")
        if not self.authenticated():
            return self.respond(401, {"error": "Bitte melde dich über /computer in Talos an."})
        try:
            if path == "/api/status":
                return self.respond(200, rpc("read", {"op": "status"}))
            if path == "/api/files":
                name = parse_qs(urlsplit(self.path).query).get("project", [""])[0]
                slug(name, "project")
                return self.respond(200, rpc("read", {"op": "files", "project": name}))
            if path == "/api/screen":
                state = rpc("read", {"op": "status"})
                if state["vm"] == "running":
                    image = Path(rpc("read", {"op": "preview"})["image_path"])
                else:
                    image = max((*CAPTURES.glob("preview-*.png"), *CAPTURES.glob("screen-*.png")),
                                key=lambda p: p.stat().st_mtime, default=None)
                if image is None:
                    return self.respond(409, {"error": "Der Computer ist angehalten."})
                if image.parent != CAPTURES or image.is_symlink():
                    raise ValueError("invalid capture")
                return self.respond(200, image.read_bytes(), "image/png")
            if path.startswith("/novnc/"):
                relative = path.removeprefix("/novnc/")
                candidate = (NOVNC / relative).resolve()
                if not candidate.is_relative_to(NOVNC.resolve()) or not candidate.is_file():
                    return self.respond(404, {"error": "not found"})
                if candidate.suffix not in {".js", ".css", ".svg", ".png", ".json"}:
                    return self.respond(404, {"error": "not found"})
                return self.respond(200, candidate.read_bytes(), mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
            return self.respond(404, {"error": "not found"})
        except (ValueError, OSError, TimeoutError):
            return self.respond(503, {"error": "Computer gerade nicht erreichbar. Dein Auftrag bleibt gespeichert."})

    def do_POST(self):
        if self.headers.get("Origin") != CONFIG["origin"]:
            return self.respond(403, {"error": "origin refused"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.respond(415, {"error": "JSON required"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 2000 or self.headers.get("Transfer-Encoding"):
                return self.respond(413, {"error": "request too large"})
            args = json.loads(self.rfile.read(size))
            if not isinstance(args, dict):
                raise ValueError("object required")
        except (ValueError, OSError):
            return self.respond(400, {"error": "invalid request"})
        path = urlsplit(self.path).path
        if path == "/api/login":
            # Same-machine proxy addresses are deliberately not treated as identities.
            now = time.time()
            if not login_allowed("/var/lib/talos-computer-web/login.db", now):
                return self.respond(429, {"error": "Bitte warte eine Minute."})
            token = args.get("token")
            if set(args) != {"token"} or not isinstance(token, str) or not hmac.compare_digest(token, CONFIG["view_secret"]):
                return self.respond(401, {"error": "Öffne den persönlichen Link aus /computer."})
            cookie = make_cookie(CONFIG["view_secret"])
            return self.respond(200, {"ok": True},
                                cookie=f"{COOKIE}={cookie}; Path=/; Max-Age=28800; Secure; HttpOnly; SameSite=Strict")
        if not self.authenticated():
            return self.respond(401, {"error": "Anmeldung erforderlich."})
        if path != "/api/control":
            return self.respond(404, {"error": "not found"})
        try:
            return self.respond(200, rpc("human", args))
        except ValueError as error:
            return self.respond(409, {"error": str(error)})
        except (OSError, TimeoutError):
            return self.respond(503, {"error": "Computer gerade nicht erreichbar."})

    def do_PUT(self):
        self.respond(405, {"error": "method refused"})

    do_DELETE = do_PUT
    do_PATCH = do_PUT


def main():
    global CONFIG
    if websocketproxy is None:
        raise SystemExit(
            "websockify is not installed. The workbench proxy runs on the Computer "
            "host, where deploy/computer-setup.py installs it."
        )
    CONFIG = json.loads(Path("/etc/talos-computer.json").read_text())
    server = websocketproxy.WebSocketProxy(
        RequestHandlerClass=Handler, listen_host="127.0.0.1", listen_port=8830,
        unix_target="/run/talos-computer-api/vnc.sock", auth_plugin=Auth(),
        web=None, daemon=False, verbose=False, heartbeat=20)
    server.start_server()


if __name__ == "__main__":
    main()
