"""Loopback-only, read-only HTTP + SSE server for ``os-optics-kit view``.

Uses only the Python standard library (``http.server.ThreadingHTTPServer``) plus the
published ``os-optics`` viewer assets (bundled Three.js + interactive 3D Optical Simulation
View). Serves GET and HEAD only; refuses non-loopback binds and mutating HTTP methods.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
import queue
import threading
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
import webbrowser

from .._engine import engine_version
from .._version import __version__ as KIT_VERSION
from .snapshot import SnapshotError
from .watcher import ViewerSession

ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ASSET_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
}
KIT_ASSETS = ("index.html", "viewer.css", "viewer.js")
ENGINE_ASSETS = ("viewer.css", "viewer.js", "three.bundle.js", "THREE_LICENSE.txt")

COMMON_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
APP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data: blob:; frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)


def _media_type(name: str) -> str:
    dot = name.rfind(".")
    return ASSET_MEDIA_TYPES.get(name[dot:] if dot >= 0 else "", "application/octet-stream")


def _kit_asset_bytes(name: str) -> bytes:
    if name not in KIT_ASSETS:
        raise KeyError(name)
    return resources.files(__package__).joinpath("assets", name).read_bytes()


def _engine_asset_bytes(name: str) -> bytes:
    from os_optics.viewer.page import asset_bytes

    return asset_bytes(name)


def _engine_index_html() -> bytes:
    html = _engine_asset_bytes("index.html").decode("utf-8")
    html = html.replace("<!--STYLE-->", '<link rel="stylesheet" href="viewer.css">')
    html = html.replace("<!--DOCUMENT-->", "")
    html = html.replace("<!--SCRIPTS-->", '<script src="three.bundle.js"></script>\n<script src="viewer.js"></script>')
    return html.encode("utf-8")


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def validate_loopback_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized not in ALLOWED_LOOPBACK_HOSTS:
        raise SnapshotError(
            "HOST_NOT_LOOPBACK",
            f"local viewer only binds loopback addresses (127.0.0.1, localhost, ::1), refused --host {host!r}",
            host=host,
        )
    return normalized


class _ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        session: ViewerSession,
        host_label: str,
        heartbeat_interval: float = 10.0,
    ) -> None:
        self.viewer_session = session
        self.host_label = host_label
        self.heartbeat_interval = heartbeat_interval
        self.engine_version_str = engine_version()
        super().__init__(server_address, handler_cls)

    def server_metadata(self) -> dict[str, Any]:
        port = self.server_address[1]
        return {
            "host": self.host_label,
            "port": port,
            "url": f"http://{self.host_label}:{port}/",
            "read_only": True,
            "poll_interval_s": self.viewer_session.poll_interval,
            "kit_version": KIT_VERSION,
            "engine_version": self.engine_version_str,
        }


class _ViewerHandler(BaseHTTPRequestHandler):
    server_version = "OsOpticsKitViewer"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    server: _ViewerHTTPServer  # type: ignore[assignment]

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", "127.0.0.1", "localhost", "[::1]"}
        return host in allowed

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        head: bool = False,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in COMMON_SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Security-Policy", APP_CSP)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)
        self.wfile.flush()

    def _send_json(
        self,
        status: HTTPStatus,
        payload: Any,
        *,
        head: bool = False,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._send_bytes(status, _json_bytes(payload), "application/json; charset=utf-8", head=head, extra_headers=extra_headers)

    def _send_redirect(
        self,
        location: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        for name, value in COMMON_SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Security-Policy", APP_CSP)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.flush()

    def _send_superseded(
        self,
        *,
        requested_snapshot_id: str | None,
        requested_revision: int | None,
        current_snapshot_id: str | None,
        current_revision: int,
        head: bool,
    ) -> None:
        self._send_json(
            HTTPStatus.CONFLICT,
            {
                "error": {
                    "code": "SNAPSHOT_SUPERSEDED",
                    "message": "requested snapshot is no longer available in the viewer cache (superseded or unknown)",
                    "requested_snapshot_id": requested_snapshot_id,
                    "requested_revision": requested_revision,
                    "current_snapshot_id": current_snapshot_id,
                    "current_revision": current_revision,
                }
            },
            head=head,
        )

    def _parse_snapshot_query(self, query: str) -> tuple[str | None, int | None, bool]:
        """Parse optional ``snapshot_id`` and ``rev`` query parameters. Returns ``(snapshot_id, rev, ok)``."""
        params = parse_qs(query, keep_blank_values=False)
        req_snap_id = params.get("snapshot_id", [None])[0]
        req_rev_raw = params.get("rev", [None])[0]
        req_rev: int | None = None
        if req_rev_raw is not None:
            try:
                req_rev = int(req_rev_raw)
            except ValueError:
                return req_snap_id, None, False
        return req_snap_id, req_rev, True

    def _serve_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

        state = self.server.viewer_session.state
        sub = state.subscribe()
        try:
            initial = state.event_payload()
            self._write_sse_event("state", initial)
            while True:
                try:
                    item = sub.get(timeout=self.server.heartbeat_interval)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                event_name, payload = item
                self._write_sse_event(event_name, payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return
        finally:
            state.unsubscribe(sub)

    def _write_sse_event(self, event_name: str, payload: dict[str, Any]) -> None:
        seq = payload.get("state_seq")
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        lines = []
        if seq is not None:
            lines.append(f"id: {seq}")
        lines.append(f"event: {event_name}")
        lines.append(f"data: {data}")
        chunk = ("\n".join(lines) + "\n\n").encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()

    def _handle(self, *, head: bool) -> None:
        if not self._host_ok():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": {"code": "FORBIDDEN_HOST", "message": "host header not allowed"}}, head=head)
            return

        parsed = urlsplit(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_bytes(HTTPStatus.OK, _kit_asset_bytes("index.html"), "text/html; charset=utf-8", head=head)
            return

        if path.startswith("/assets/"):
            name = path[len("/assets/"):]
            if name in KIT_ASSETS and name != "index.html":
                self._send_bytes(HTTPStatus.OK, _kit_asset_bytes(name), _media_type(name), head=head)
                return
            if name == "three.bundle.js":
                self._send_bytes(HTTPStatus.OK, _engine_asset_bytes("three.bundle.js"), _media_type(name), head=head)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": f"unknown asset: {name}"}}, head=head)
            return

        if path == "/api/health":
            ev = self.server.viewer_session.state.event_payload()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "status": ev["status"],
                    "revision": ev["revision"],
                    "state_seq": ev["state_seq"],
                    "result_mode": ev["result_mode"],
                    "read_only": True,
                    "kit_version": KIT_VERSION,
                    "engine_version": self.server.engine_version_str,
                },
                head=head,
            )
            return

        if path == "/api/snapshot":
            envelope = self.server.viewer_session.state.envelope(self.server.server_metadata())
            active = envelope.get("snapshot") or envelope.get("last_valid_snapshot")
            snap_headers: list[tuple[str, str]] = [("X-Viewer-Revision", str(envelope["revision"]))]
            if isinstance(active, dict) and active.get("snapshot_id"):
                snap_headers.append(("X-Viewer-Snapshot-Id", str(active["snapshot_id"])))
            self._send_json(HTTPStatus.OK, envelope, head=head, extra_headers=tuple(snap_headers))
            return

        if path == "/api/scene-document":
            req_snap_id, req_rev, query_ok = self._parse_snapshot_query(parsed.query)
            if not query_ok:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "INVALID_REVISION", "message": "rev query parameter must be an integer"}},
                    head=head,
                )
                return
            state = self.server.viewer_session.state
            if req_snap_id is not None or req_rev is not None:
                built, cur_id, cur_rev = state.get_cached_snapshot(snapshot_id=req_snap_id, revision=req_rev)
                if built is None or built.scene_document is None:
                    self._send_superseded(
                        requested_snapshot_id=req_snap_id,
                        requested_revision=req_rev,
                        current_snapshot_id=cur_id,
                        current_revision=cur_rev,
                        head=head,
                    )
                    return
                doc_headers: tuple[tuple[str, str], ...] = (
                    ("X-Viewer-Revision", str(built.snapshot["revision"])),
                    ("X-Viewer-Snapshot-Id", str(built.snapshot["snapshot_id"])),
                )
                self._send_json(HTTPStatus.OK, built.scene_document, head=head, extra_headers=doc_headers)
                return

            doc, rev, snap_id = state.scene_document()
            if doc is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "SCENE_UNAVAILABLE", "message": "no valid scene document available yet"}}, head=head)
                return
            doc_headers = (
                ("X-Viewer-Revision", str(rev)),
                ("X-Viewer-Snapshot-Id", snap_id or ""),
            )
            self._send_json(HTTPStatus.OK, doc, head=head, extra_headers=doc_headers)
            return

        if path == "/api/events":
            if head:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            self._serve_sse()
            return

        if path in ("/engine-view", "/engine-view/"):
            req_snap_id, req_rev, query_ok = self._parse_snapshot_query(parsed.query)
            if not query_ok:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "INVALID_REVISION", "message": "rev query parameter must be an integer"}},
                    head=head,
                )
                return
            state = self.server.viewer_session.state
            if req_snap_id is not None or req_rev is not None:
                built, cur_id, cur_rev = state.get_cached_snapshot(snapshot_id=req_snap_id, revision=req_rev)
                if built is None or built.scene_document is None:
                    self._send_superseded(
                        requested_snapshot_id=req_snap_id,
                        requested_revision=req_rev,
                        current_snapshot_id=cur_id,
                        current_revision=cur_rev,
                        head=head,
                    )
                    return
            else:
                built, _cur_id, _cur_rev = state.get_cached_snapshot()
                if built is None or built.scene_document is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": {"code": "SCENE_UNAVAILABLE", "message": "no valid scene document available yet"}},
                        head=head,
                    )
                    return
            exact_id = str(built.snapshot["snapshot_id"])
            exact_rev = str(built.snapshot["revision"])
            self._send_redirect(
                f"/engine-view/{quote(exact_id, safe='')}/",
                extra_headers=(
                    ("X-Viewer-Revision", exact_rev),
                    ("X-Viewer-Snapshot-Id", exact_id),
                ),
            )
            return

        if path.startswith("/engine-view/"):
            rest = path[len("/engine-view/"):]
            state = self.server.viewer_session.state
            if "/" in rest:
                raw_snap_id, asset = rest.split("/", 1)
                req_snap_id = unquote(raw_snap_id)
                asset = asset or "index.html"
                if asset not in (*ENGINE_ASSETS, "index.html", "view.json"):
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": {"code": "NOT_FOUND", "message": f"unknown engine-view asset: {asset}"}},
                        head=head,
                    )
                    return
                built, cur_id, cur_rev = state.get_cached_snapshot(snapshot_id=req_snap_id)
                if built is None or built.scene_document is None:
                    self._send_superseded(
                        requested_snapshot_id=req_snap_id,
                        requested_revision=None,
                        current_snapshot_id=cur_id,
                        current_revision=cur_rev,
                        head=head,
                    )
                    return
                pinned_headers: tuple[tuple[str, str], ...] = (
                    ("X-Viewer-Revision", str(built.snapshot["revision"])),
                    ("X-Viewer-Snapshot-Id", str(built.snapshot["snapshot_id"])),
                )
                if asset in ENGINE_ASSETS:
                    self._send_bytes(
                        HTTPStatus.OK,
                        _engine_asset_bytes(asset),
                        _media_type(asset),
                        head=head,
                        extra_headers=pinned_headers,
                    )
                    return
                if asset == "index.html":
                    self._send_bytes(
                        HTTPStatus.OK,
                        _engine_index_html(),
                        "text/html; charset=utf-8",
                        head=head,
                        extra_headers=pinned_headers,
                    )
                    return
                self._send_json(HTTPStatus.OK, built.scene_document, head=head, extra_headers=pinned_headers)
                return

            # Unversioned asset paths under /engine-view/ are never served.
            if rest in (*ENGINE_ASSETS, "index.html", "view.json"):
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": {
                            "code": "NOT_FOUND",
                            "message": f"unversioned /engine-view/{rest} is not served; use /engine-view/<snapshot_id>/",
                        }
                    },
                    head=head,
                )
                return

            # Bare /engine-view/<snapshot_id> without trailing slash: validate cache before redirecting.
            req_snap_id = unquote(rest)
            built, cur_id, cur_rev = state.get_cached_snapshot(snapshot_id=req_snap_id)
            if built is None or built.scene_document is None:
                self._send_superseded(
                    requested_snapshot_id=req_snap_id,
                    requested_revision=None,
                    current_snapshot_id=cur_id,
                    current_revision=cur_rev,
                    head=head,
                )
                return
            exact_id = str(built.snapshot["snapshot_id"])
            exact_rev = str(built.snapshot["revision"])
            self._send_redirect(
                f"/engine-view/{quote(exact_id, safe='')}/",
                extra_headers=(
                    ("X-Viewer-Revision", exact_rev),
                    ("X-Viewer-Snapshot-Id", exact_id),
                ),
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "NOT_FOUND", "message": f"not found: {path}"}}, head=head)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(head=True)

    def _refuse(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if 0 < length <= 65536:
            try:
                self.rfile.read(length)
            except OSError:
                pass
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": {"code": "METHOD_NOT_ALLOWED", "message": "local viewer is strictly read-only (GET/HEAD only)"}},
        )

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


class LocalViewerServer:
    """Loopback read-only HTTP + SSE server backed by a live ``ViewerSession``."""

    def __init__(
        self,
        session: ViewerSession,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        heartbeat_interval: float = 10.0,
    ) -> None:
        host_ok = validate_loopback_host(host)
        if not isinstance(port, int) or not (0 <= port <= 65535):
            raise SnapshotError("USAGE", f"--port must be an integer between 0 and 65535, got {port!r}")
        bind_host = "127.0.0.1" if host_ok == "localhost" else host_ok
        self.session = session
        self._httpd = _ViewerHTTPServer(
            (bind_host, port),
            _ViewerHandler,
            session=session,
            host_label=host_ok,
            heartbeat_interval=heartbeat_interval,
        )
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="os-optics-kit-viewer-http", daemon=True)
        self._thread.start()
        self._closed = False

    @property
    def host(self) -> str:
        return self._httpd.host_label

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def startup_info(self) -> dict[str, Any]:
        ev = self.session.state.event_payload()
        snap = self.session.state.envelope().get("snapshot") or {}
        return {
            "status": "READY",
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "read_only": True,
            "revision": ev["revision"],
            "snapshot_id": ev["snapshot_id"],
            "result_mode": ev["result_mode"],
            "is_computed_result": ev["is_computed_result"],
            "freshness_state": ev["freshness_state"],
            "source": snap.get("source"),
            "endpoints": {
                "ui": self.url,
                "snapshot": f"{self.url}api/snapshot",
                "events": f"{self.url}api/events",
                "scene_document": f"{self.url}api/scene-document",
                "engine_view": f"{self.url}engine-view/",
                "health": f"{self.url}api/health",
            },
        }

    def open_browser(self) -> bool:
        try:
            return bool(webbrowser.open(self.url, new=2))
        except webbrowser.Error:
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.session.close()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self) -> LocalViewerServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
