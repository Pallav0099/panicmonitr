"""Local control socket (HTTP-over-Unix) for the running daemon.

Mirrors the pattern in ``statuspage.py``: a small ``ThreadingHTTPServer``
bound to an ``AF_UNIX`` socket, running in a daemon thread. The CLI talks
to it via ``urllib.request`` over the same Unix socket so admin commands
take effect against the live daemon instead of having to mutate state
files behind its back.

Auth = ``SO_PEERCRED`` + filesystem mode (0600, owned by the daemon's
user). No tokens.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import struct
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, urlparse

from loguru import logger

from src import paths

if TYPE_CHECKING:
    from src.engine import MonitorEngine


def _peer_euid(sock: socket.socket) -> int | None:
    """Return the peer's effective UID using ``SO_PEERCRED`` (Linux)."""
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except OSError:
        return None


class _UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """``HTTPServer`` over ``AF_UNIX``. Overrides bind to handle a path
    instead of a (host, port) tuple, and chmods the socket to 0600 so the
    filesystem layer enforces owner-only access.
    """

    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, socket_path: str, handler_cls):
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        # bind_and_activate=True drives server_bind() + server_activate()
        # below, which know about AF_UNIX.
        super().__init__(socket_path, handler_cls, bind_and_activate=True)
        os.chmod(socket_path, 0o600)

    def server_bind(self):
        # Skip HTTPServer's hostname plumbing (which assumes AF_INET).
        self.socket.bind(self.server_address)


class _ControlHandler(http.server.BaseHTTPRequestHandler):
    """RPC handler. Endpoints below match the inventory in the overhaul plan."""

    engine: "MonitorEngine"  # late-bound on the class object by start()
    server_version = "panic-monitor-controlsock/1"

    # AF_UNIX gives '' as client_address; base address_string would index [0]
    # into that empty string. Override to return a stable label.
    def address_string(self) -> str:  # noqa: D401
        return "unix"

    # Quiet the default access logger; we have our own.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: D401, A002
        logger.debug("[controlsock] {}", format % args)

    # --- auth -------------------------------------------------------------

    def _authorized(self) -> bool:
        uid = _peer_euid(self.request)
        if uid is None:
            self._send_error(403, "peer credentials unavailable")
            return False
        if uid != os.geteuid():
            self._send_error(403, f"peer euid {uid} != daemon euid {os.geteuid()}")
            return False
        return True

    # --- response helpers -------------------------------------------------

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}")

    # --- dispatch ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — http.server requires this name
        if not self._authorized():
            return
        try:
            self._route("GET")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[controlsock] GET {} failed", self.path)
            self._send_error(500, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        try:
            self._route("POST")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[controlsock] POST {} failed", self.path)
            self._send_error(500, str(exc))

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        try:
            self._route("PUT")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[controlsock] PUT {} failed", self.path)
            self._send_error(500, str(exc))

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        try:
            self._route("DELETE")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[controlsock] DELETE {} failed", self.path)
            self._send_error(500, str(exc))

    def _route(self, method: str) -> None:
        engine = type(self).engine
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = path.lstrip("/").split("/")

        # /v1/identity
        if path == "/v1/identity" and method == "GET":
            return self._send_json(_identity_view(engine))

        # /v1/status
        if path == "/v1/status" and method == "GET":
            from src.statuspage import build_dashboard_snapshot
            return self._send_json(build_dashboard_snapshot(engine))

        # /v1/reload
        if path == "/v1/reload" and method == "POST":
            engine._check_reload()
            return self._send_json({"ok": True})

        # /v1/peers + /v1/peers/<nid>...
        if parts[:2] == ["v1", "peers"]:
            return self._route_peers(method, parts[2:])

        self._send_error(404, f"unknown endpoint {path}")

    def _route_peers(self, method: str, rest: list[str]) -> None:
        engine = type(self).engine
        # Collection
        if not rest:
            if method == "GET":
                peers = [_peer_view(p) for p in engine.trust.list_peers()]
                return self._send_json({"peers": peers})
            if method == "POST":
                body = self._read_body()
                if not engine.verify_identity_password(body.get("password") or ""):
                    return self._send_error(403, "password verification failed")
                node_id = body.get("node_id")
                alias = body.get("alias")
                perms = body.get("permissions") or ["monitor"]
                tags = body.get("tags") or []
                err = engine.add_peer(node_id, alias, perms, tags=tags)
                if err:
                    return self._send_error(400, err)
                return self._send_json({"ok": True, "node_id": node_id}, status=201)
            return self._send_error(405, f"method {method} not allowed")

        nid = rest[0]
        sub = rest[1] if len(rest) > 1 else None

        if sub is None:
            if method == "DELETE":
                err = engine.revoke_peer(nid)
                if err:
                    return self._send_error(400, err)
                return self._send_json({"ok": True})
            if method == "GET":
                peer = engine.trust.get_peer(nid)
                if peer is None:
                    return self._send_error(404, "peer not found")
                return self._send_json(_peer_view(peer))
            return self._send_error(405, f"method {method} not allowed")

        if sub == "perms":
            if method != "PUT":
                return self._send_error(405, "use PUT")
            body = self._read_body()
            if not engine.verify_identity_password(body.get("password") or ""):
                return self._send_error(403, "password verification failed")
            perms = body.get("permissions")
            if not isinstance(perms, list):
                return self._send_error(400, "permissions must be a list")
            err = engine.update_peer_permissions(nid, perms)
            return self._reply(err)

        if sub == "tags":
            body = self._read_body() if method != "DELETE" else {}
            if method == "PUT":
                tags = body.get("tags")
                if not isinstance(tags, list):
                    return self._send_error(400, "tags must be a list")
                err = engine.set_peer_tags(nid, tags)
                return self._reply(err)
            if method == "POST":
                tag = body.get("tag")
                if not isinstance(tag, str) or not tag:
                    return self._send_error(400, "tag must be a non-empty string")
                err = engine.add_peer_tag(nid, tag)
                return self._reply(err)
            if method == "DELETE":
                qs = parse_qs(urlparse(self.path).query)
                tag = (qs.get("tag") or [None])[0]
                if not tag:
                    body = self._read_body() if not body else body
                    tag = body.get("tag")
                if not tag:
                    return self._send_error(400, "tag must be specified")
                err = engine.remove_peer_tag(nid, tag)
                return self._reply(err)
            return self._send_error(405, f"method {method} not allowed")

        if sub == "maint":
            if method == "PUT":
                body = self._read_body()
                start_raw = body.get("start")
                end_raw = body.get("end")
                if not start_raw or not end_raw:
                    return self._send_error(400, "start and end required (ISO 8601)")
                try:
                    start = datetime.fromisoformat(start_raw)
                    end = datetime.fromisoformat(end_raw)
                except ValueError as exc:
                    return self._send_error(400, f"invalid timestamp: {exc}")
                err = engine.set_peer_maintenance(nid, start, end)
                return self._reply(err)
            if method == "DELETE":
                err = engine.clear_peer_maintenance(nid)
                return self._reply(err)
            return self._send_error(405, f"method {method} not allowed")

        if sub == "sync":
            if method != "POST":
                return self._send_error(405, "use POST")
            body = self._read_body()
            since_raw = body.get("since")
            since = None
            if since_raw:
                try:
                    since = datetime.fromisoformat(since_raw)
                except ValueError as exc:
                    return self._send_error(400, f"invalid since: {exc}")
            # sync_peer is added in P3.5a
            try:
                import asyncio
                fut = asyncio.run_coroutine_threadsafe(
                    engine.sync_peer(nid, since), engine.loop
                )
                result = fut.result(timeout=60)
                return self._send_json(result)
            except AttributeError:
                return self._send_error(501, "sync_peer not implemented")
            except Exception as exc:  # noqa: BLE001
                return self._send_error(500, f"sync failed: {exc}")

        if sub == "dashboard":
            if method != "GET":
                return self._send_error(405, "use GET")
            try:
                import asyncio
                fut = asyncio.run_coroutine_threadsafe(
                    engine.fetch_peer_dashboard(nid), engine.loop
                )
                result = fut.result(timeout=60)
                return self._send_json(result)
            except Exception as exc:  # noqa: BLE001
                return self._send_error(500, f"fetch failed: {exc}")

        self._send_error(404, f"unknown peer endpoint /{sub}")

    def _reply(self, err: str | None) -> None:
        if err:
            return self._send_error(400, err)
        return self._send_json({"ok": True})


def _peer_view(peer) -> dict:
    return {
        "node_id": peer.node_id,
        "alias": peer.alias,
        "permissions": list(peer.permissions),
        "tags": list(peer.tags),
        "added_at": peer.added_at.isoformat() if peer.added_at else None,
        "revoked_at": peer.revoked_at.isoformat() if peer.revoked_at else None,
        "maintenance_start": peer.maintenance_start.isoformat() if peer.maintenance_start else None,
        "maintenance_end": peer.maintenance_end.isoformat() if peer.maintenance_end else None,
    }


def _identity_view(engine: "MonitorEngine") -> dict:
    return {
        "node_id": engine.node_id,
        "role": engine.role.value,
        "config_dir": str(paths.config_dir()),
        "data_dir": str(paths.data_dir()),
        "runtime_dir": str(paths.runtime_dir()),
    }


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):  # enum
        return obj.value
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


class ControlSocketServer:
    """Daemon-thread wrapper around the Unix-domain HTTP server."""

    def __init__(self, engine: "MonitorEngine", socket_path: str | None = None) -> None:
        self._engine = engine
        self._socket_path = socket_path or str(paths.default_control_socket())
        self._server: _UnixHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def start(self) -> None:
        paths.ensure_runtime_dir()

        class BoundHandler(_ControlHandler):
            pass

        BoundHandler.engine = self._engine
        try:
            self._server = _UnixHTTPServer(self._socket_path, BoundHandler)
        except OSError as exc:
            logger.error("[controlsock] bind {} failed: {}", self._socket_path, exc)
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="panic-monitor-controlsock",
            daemon=True,
        )
        self._thread.start()
        logger.info("[controlsock] listening on {}", self._socket_path)

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[controlsock] shutdown error: {}", exc)
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass
        self._server = None
        self._thread = None
