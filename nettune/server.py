"""Local HTTP server: serves the dashboard and the JSON API.

Security model (the engine can change system settings, so this matters):

* Binds to 127.0.0.1 only - never reachable from the network.
* Every request must carry a ``Host`` header for 127.0.0.1/localhost on our
  port, which defeats DNS-rebinding attacks from websites.
* Every API call must carry a random per-run token in a custom header. Other
  websites cannot read the token (same-origin policy) nor send the header
  (no CORS is granted), which defeats cross-site request forgery.
* The API exposes a fixed list of actions; it never runs caller-supplied
  commands or accepts arbitrary values such as DNS server addresses.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .engine import ApiError, Engine
from .system import is_admin

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 64 * 1024
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json",
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class NetTuneServer(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows, reuse would let two servers share the port
    daemon_threads = True

    def __init__(self, address, engine: Engine):
        super().__init__(address, Handler)
        self.engine = engine
        self.token = secrets.token_urlsafe(32)
        port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}


class Handler(BaseHTTPRequestHandler):
    server_version = "NetTune/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -- responses -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), MIME[".json"])

    # -- request handling ----------------------------------------------------
    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, "Request too large.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, "Request body must be JSON.") from exc
        return data if isinstance(data, dict) else {}

    def _dispatch(self) -> None:
        server: NetTuneServer = self.server  # type: ignore[assignment]
        if self.headers.get("Host", "") not in server.allowed_hosts:
            self._json(403, {"error": "Requests must be addressed to 127.0.0.1."})
            return
        path = urlsplit(self.path).path
        if path == "/api/ping":
            # Unauthenticated liveness probe used while the engine restarts elevated.
            self._json(200, {"ok": True, "admin": is_admin()})
            return
        if path.startswith("/api/"):
            self._api(server, path)
        elif self.command in ("GET", "HEAD"):
            self._static(server, path)
        else:
            self._json(405, {"error": "Method not allowed."})

    def _api(self, server: NetTuneServer, path: str) -> None:
        origin = self.headers.get("Origin")
        if origin and origin not in server.allowed_origins:
            self._json(403, {"error": "Cross-origin requests are not allowed."})
            return
        if not hmac.compare_digest(self.headers.get("X-NetTune-Token", ""), server.token):
            self._json(401, {"error": "Missing or stale session token. Reload the page.", "code": "token"})
            return
        try:
            result = server.engine.handle(self.command, path, self._read_body())
            self._json(200, result)
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc), "code": exc.code})
        except Exception as exc:  # last resort: report, keep serving
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"error": f"Internal error: {exc}", "code": "internal"})

    def _static(self, server: NetTuneServer, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR not in target.parents or not target.is_file() or target.suffix not in MIME:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        body = target.read_bytes()
        if target.name == "index.html":
            body = body.replace(b"__NETTUNE_TOKEN__", server.token.encode("ascii"))
        self._send(200, body, MIME[target.suffix])

    do_GET = do_HEAD = do_POST = do_DELETE = _dispatch


def create_server(engine: Engine, port: int, takeover: bool = False) -> NetTuneServer:
    """Bind to ``port`` (or the next free one). ``takeover`` waits for the port to free up."""
    last_error = None
    if takeover:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                return _bind(engine, port)
            except OSError as exc:
                last_error = exc
                time.sleep(0.4)
    else:
        for candidate in range(port, port + 15):
            try:
                return _bind(engine, candidate)
            except OSError as exc:
                last_error = exc
    raise OSError(f"Could not open a local port starting at {port}: {last_error}")


def _bind(engine: Engine, port: int) -> NetTuneServer:
    server = NetTuneServer(("127.0.0.1", port), engine)
    engine.port = server.server_address[1]
    engine.on_shutdown = server.shutdown
    return server
