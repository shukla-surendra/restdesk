"""RestDesk - a tiny, local, dependency-free REST client.

Run:  uv run restdesk                 (opens http://127.0.0.1:8787)
      uv run restdesk --port 9000 --data ./my-restdesk.db --no-browser

The browser UI talks to this process, which performs the real HTTP calls.
That avoids CORS limits and lets you hit any API (localhost, internal, public).
Collections, environments and the full call history are stored in SQLite.
Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .storage import Conflict, SchemaTooNew, Store

VERSION = __version__
STATIC = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 50 * 1024 * 1024   # body we accept from the UI
MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # body we read from the target API
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

# The UI page may only talk to this server and run its own files (no inline/remote script).
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; worker-src 'self'; frame-src 'none'; "
            "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# User scripts run in this worker. A worker obeys the CSP sent with its own script, so this
# leaves it able to compute (eval) but not to make network requests or load more code.
SANDBOX_CSP = "default-src 'none'; script-src 'unsafe-eval'"
SANDBOX_FILE = "sandbox-worker.js"


def default_data_path() -> Path:
    """Per-user location, so `uv tool install` / `uvx` runs keep their data."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "restdesk" / "restdesk.db"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses as-is instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _decompress(data: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower().strip()
    try:
        if enc in ("gzip", "x-gzip"):
            return zlib.decompress(data, 16 + zlib.MAX_WBITS)
        if enc == "deflate":
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)
    except zlib.error:
        pass
    return data


def perform_request(spec: dict) -> dict:
    method = str(spec.get("method") or "GET").upper()
    url = str(spec.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}

    data = None
    if spec.get("bodyBase64"):
        data = base64.b64decode(spec["bodyBase64"])
    elif spec.get("body") is not None:
        data = str(spec["body"]).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    has_ua = False
    for pair in spec.get("headers") or []:
        if not pair or not str(pair[0]).strip():
            continue
        key, value = str(pair[0]).strip(), str(pair[1] if len(pair) > 1 else "")
        if key.lower() == "user-agent":
            has_ua = True
        req.add_header(key, value)
    if not has_ua:
        req.add_header("User-Agent", f"RestDesk/{VERSION}")

    ctx = ssl.create_default_context()
    if spec.get("verifyTls") is False:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers: list = [urllib.request.HTTPSHandler(context=ctx)]
    if spec.get("followRedirects") is False:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)

    timeout = max(0.1, float(spec.get("timeoutMs") or 30000) / 1000.0)
    started = time.perf_counter()
    try:
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as err:  # 4xx/5xx/3xx(no-follow) still have bodies
            resp = err
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
        elapsed = (time.perf_counter() - started) * 1000.0
        truncated = len(raw) > MAX_RESPONSE_BYTES
        raw = raw[:MAX_RESPONSE_BYTES]
        headers = list(resp.headers.items())
        wire_size = len(raw)
        raw = _decompress(raw, resp.headers.get("Content-Encoding", ""))
        status = getattr(resp, "status", None) or getattr(resp, "code", 0)
        reason = getattr(resp, "reason", "") or ""
        final_url = resp.geturl() if hasattr(resp, "geturl") else url
        resp.close()
    except Exception as exc:  # DNS, refused, TLS, timeout, ...
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}",
            "timeMs": round((time.perf_counter() - started) * 1000.0, 1),
        }

    try:
        body, encoding = raw.decode("utf-8"), "utf8"
    except UnicodeDecodeError:
        body, encoding = base64.b64encode(raw).decode("ascii"), "base64"

    return {
        "ok": True,
        "status": status,
        "reason": str(reason),
        "headers": headers,
        "body": body,
        "bodyEncoding": encoding,
        "size": len(raw),
        "wireSize": wire_size,
        "truncated": truncated,
        "timeMs": round(elapsed, 1),
        "url": final_url,
    }




def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"RestDesk/{VERSION}"

        def log_message(self, fmt, *args):  # quieter console
            if os.environ.get("RESTDESK_VERBOSE"):
                super().log_message(fmt, *args)

        # ---------- helpers ----------
        def _send(self, code: int, payload, ctype="application/json; charset=utf-8", extra=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            # Defends against DNS-rebinding: only answer to local host names.
            raw = self.headers.get("Host") or ""
            host = raw.split("]")[0] + "]" if raw.startswith("[") else raw.rsplit(":", 1)[0]
            return host in ALLOWED_HOSTS

        def _api_ok(self) -> bool:
            # The custom header forces a CORS preflight that we never approve,
            # so other web pages in your browser cannot drive this proxy.
            return self._host_ok() and self.headers.get("X-RestDesk") == "1"

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(length) or b"{}")

        def _route(self):
            parts = urllib.parse.urlsplit(self.path)
            return parts.path.rstrip("/") or "/", urllib.parse.parse_qs(parts.query)

        # ---------- routes ----------
        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden host"})
            path, qs = self._route()
            if path.startswith("/api/"):
                if not self._api_ok():
                    return self._send(403, {"error": "forbidden"})
                one = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731
                if path == "/api/workspace":
                    return self._send(200, store.load_workspace())
                if path == "/api/history":
                    return self._send(200, store.list_history(
                        limit=int(one("limit", "100") or 100), offset=int(one("offset", "0") or 0),
                        q=one("q"), request_id=one("requestId") or None))
                if path.startswith("/api/history/"):
                    entry = store.get_history(path.rsplit("/", 1)[1])
                    return self._send(200, entry) if entry else self._send(404, {"error": "not found"})
                if path == "/api/export":
                    data = store.export_all(include_history=one("history", "1") != "0")
                    return self._send(200, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))
                if path == "/api/info":
                    return self._send(200, {"version": VERSION, "dataPath": str(store.path),
                                            "historyLimit": store.history_limit,
                                            "redactHistory": store.redact_history,
                                            "schemaVersion": store.schema_version})
                if path == "/api/trash":
                    return self._send(200, {"items": store.list_trash()})
                return self._send(404, {"error": "not found"})
            if path in ("/", "/index.html"):
                path = "/index.html"
            target = (STATIC / path.lstrip("/")).resolve()
            if STATIC not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            csp = SANDBOX_CSP if target.name == SANDBOX_FILE else PAGE_CSP
            return self._send(200, target.read_bytes(), ctype, {"Content-Security-Policy": csp})

        def do_PUT(self):
            path, _ = self._route()
            if path != "/api/workspace" or not self._api_ok():
                return self._send(403, {"error": "forbidden"})
            try:
                ws = self._read_json()
                base = ws.pop("baseRevision", None) if isinstance(ws, dict) else None
                if isinstance(ws, dict):
                    ws.pop("revision", None)
                revision = store.save_workspace(ws, base_revision=base)
            except Conflict as exc:
                return self._send(409, {"error": "conflict", "revision": exc.revision})
            except Exception as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, {"saved": True, "revision": revision})

        def do_POST(self):
            path, _ = self._route()
            if not self._api_ok():
                return self._send(403, {"error": "forbidden"})
            try:
                payload = self._read_json()
            except Exception as exc:
                return self._send(400, {"error": str(exc)})
            if path == "/api/send":
                return self._send(200, perform_request(payload))
            if path == "/api/history":
                try:
                    return self._send(200, store.add_history(payload))
                except Exception as exc:
                    return self._send(400, {"error": str(exc)})
            if path.startswith("/api/trash/") and path.endswith("/restore"):
                item = store.take_from_trash(path.split("/")[3])
                return self._send(200, item) if item else self._send(404, {"error": "not found"})
            return self._send(404, {"error": "not found"})

        def do_DELETE(self):
            path, _ = self._route()
            if not self._api_ok():
                return self._send(403, {"error": "forbidden"})
            if path == "/api/history":
                return self._send(200, {"deleted": store.delete_history()})
            if path.startswith("/api/history/"):
                return self._send(200, {"deleted": store.delete_history(path.rsplit("/", 1)[1])})
            if path == "/api/trash":
                return self._send(200, {"deleted": store.empty_trash()})
            return self._send(404, {"error": "not found"})

    return Handler


def _migrate_legacy(store: Store, explicit: str | None) -> None:
    """On first run, pull in an old 0.1 `data/workspace.json` if one is around."""
    if not store.is_empty():
        return
    candidates = [Path(explicit)] if explicit else []
    candidates += [Path.cwd() / "data" / "workspace.json"]
    for cand in candidates:
        cand = cand.expanduser()
        if cand.is_file() and store.import_legacy_json(cand):
            print(f"Imported legacy workspace from {cand}")
            return


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="restdesk", description="RestDesk - lightweight local REST client")
    ap.add_argument("--version", action="version", version=f"RestDesk {VERSION}")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RESTDESK_PORT", 8787)))
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (keep 127.0.0.1 - this is an HTTP proxy)")
    ap.add_argument("--data", default=os.environ.get("RESTDESK_DATA", str(default_data_path())),
                    help="SQLite database file (collections, environments, call history)")
    ap.add_argument("--history-limit", type=int, default=int(os.environ.get("RESTDESK_HISTORY_LIMIT", 2000)),
                    help="number of calls to keep in history (0 = unlimited, default 2000)")
    ap.add_argument("--keep-secrets-in-history", action="store_true",
                    default=os.environ.get("RESTDESK_KEEP_SECRETS") == "1",
                    help="store tokens/passwords in call history unmasked (default: redact them)")
    ap.add_argument("--import-json", metavar="FILE",
                    help="import a RestDesk 0.1 workspace.json into an empty database")
    ap.add_argument("--export", metavar="FILE",
                    help="write collections, environments and history to a JSON file and exit")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args(argv)

    data_path = Path(args.data).expanduser().resolve()
    try:
        store = Store(data_path, history_limit=max(0, args.history_limit),
                      redact_history=not args.keep_secrets_in_history)
    except SchemaTooNew as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.export:
        Path(args.export).expanduser().write_text(
            json.dumps(store.export_all(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Exported {data_path} -> {args.export}")
        return 0

    _migrate_legacy(store, args.import_json)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"RestDesk {VERSION} running at {url}")
    print(f"Database: {data_path}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
