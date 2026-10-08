"""`jevloop serve`: a tiny static file server for the dashboard.

Serves the dashboard HTML files alongside ~/.jev-loop/latest.json so
dashboard/index.html and dashboard/wall.html can poll it with a plain
fetch(). No framework, no build step: http.server with two directories
merged via a symlink-free request handler.

Start / Stop: GET /control returns {"state": "RUNNING" | "PAUSED"}; POST
/control with {"state": ...} changes it. The server only listens on
127.0.0.1. A POST needs the per-install token from ~/.jev-loop/control.token
in an X-Jev-Token header, which the server writes into the dashboard page
it serves; a page on any other site can't read it (no CORS headers are ever
sent) and can't send that header without a CORS preflight, which is
refused. The Host header must be 127.0.0.1 or localhost, so a website
that re-points its own name at 127.0.0.1 (DNS rebinding) is refused too.
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import json
import os
import socketserver
import time
from pathlib import Path

from . import control

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
SKILL_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = SKILL_DIR / "dashboard"


_last_good: dict[str, bytes] = {}


def read_feed(path: Path, attempts: int = 5, wait_s: float = 0.02) -> bytes | None:
    """Read a feed file in one go and close it straight away, so the server
    never holds latest.json open while the loop swaps in a new copy (on
    Windows an open file blocks that swap). A read that hits the swap
    mid-way (PermissionError, or JSON cut short) is retried briefly, then
    falls back to the last good copy served, never an error page."""
    for _ in range(attempts):
        try:
            data = path.read_bytes()
            if path.suffix == ".json":
                json.loads(data)  # a partial write fails here, not in the browser
            _last_good[path.name] = data
            return data
        except FileNotFoundError:
            break
        except (PermissionError, ValueError):
            time.sleep(wait_s)
    return _last_good.get(path.name)


TOKEN_PLACEHOLDER = b"__JEV_CONTROL_TOKEN__"
MAX_CONTROL_BODY = 1024


class Handler(http.server.SimpleHTTPRequestHandler):
    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code: int, data: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_state(self, code: int = 200) -> None:
        self._send(code, json.dumps({"state": control.read_state(LOG_DIR)}).encode())

    def _refuse(self, code: int, why: str) -> None:
        self._send(code, json.dumps({"error": why}).encode())

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path != "/control":
            return self._refuse(404, "not found")
        if not self._host_ok():
            return self._refuse(403, "bad host")
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self._refuse(403, "bad origin")
        sent = self.headers.get("X-Jev-Token", "")
        token = control.load_or_create_token(LOG_DIR)
        if not hmac.compare_digest(sent.encode(), token.encode()):
            return self._refuse(403, "bad token")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_CONTROL_BODY:
                raise ValueError
            state = json.loads(self.rfile.read(length) or b"{}").get("state")
            if state not in control.STATES:
                raise ValueError
        except (ValueError, AttributeError):
            return self._refuse(400, 'send {"state": "RUNNING"} or {"state": "PAUSED"}')
        try:
            control.write_state(LOG_DIR, state)
        except PermissionError:
            return self._refuse(503, "control file busy, try again")
        self._send_state()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/control":
            if not self._host_ok():
                return self._refuse(403, "bad host")
            return self._send_state()
        if path in ("/", "/index.html"):
            # The Start/Stop token goes into the page, and only for a
            # request addressed to this machine by name or IP.
            if not self._host_ok():
                return self._refuse(403, "bad host")
            try:
                page = (DASHBOARD_DIR / "index.html").read_bytes()
            except FileNotFoundError:
                return self._refuse(404, "dashboard/index.html missing")
            page = page.replace(TOKEN_PLACEHOLDER, control.load_or_create_token(LOG_DIR).encode())
            return self._send(200, page, "text/html; charset=utf-8")
        if path not in ("/latest.json", "/log.jsonl"):
            return super().do_GET()
        data = read_feed(LOG_DIR / path.lstrip("/"))
        if data is None:
            data = b'{"ticks": [], "stats": {}}' if path == "/latest.json" else b""
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json" if path == "/latest.json" else "text/plain",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/latest.json", "/log.jsonl"):
            return str(LOG_DIR / path.lstrip("/"))
        if path == "/":
            path = "/index.html"
        candidate = DASHBOARD_DIR / path.lstrip("/")
        return str(candidate)

    def log_message(self, format, *args):  # noqa: A002
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop serve")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    latest = LOG_DIR / "latest.json"
    if not latest.exists():
        latest.write_text('{"ticks": [], "stats": {}}')
    control.load_or_create_token(LOG_DIR)

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"dashboard: http://127.0.0.1:{args.port}/index.html")
        print(f"dark wall: http://127.0.0.1:{args.port}/wall.html")
        print(f"raw feed:  http://127.0.0.1:{args.port}/latest.json")
        print(f"control:   {control.read_state(LOG_DIR)} (Start/Stop buttons on the dashboard)")
        print("Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
