"""Tiny HTTP-over-Unix-socket client for talking to the running daemon.

Sized for the CLI's needs: build a request, send it, read the response, raise
on non-2xx. No keep-alive. No async. The control socket is a local admin
surface; a one-shot per command is fine.
"""
from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any

from src import paths


class ControlClientError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"control socket error {status}: {message}")
        self.status = status
        self.message = message


class _UnixHTTPConnection(http.client.HTTPConnection):
    """``HTTPConnection`` that dials a Unix socket instead of TCP."""

    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def socket_path() -> Path:
    return paths.default_control_socket()


def available() -> bool:
    p = socket_path()
    return p.exists() and p.is_socket()


def request(
    method: str,
    path: str,
    body: Any = None,
    *,
    sock: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """Send ``method path`` over the control socket. Returns parsed JSON.

    Raises ``ControlClientError`` for non-2xx responses.
    """
    sp = sock or str(socket_path())
    conn = _UnixHTTPConnection(sp, timeout=timeout)
    try:
        headers = {"Accept": "application/json"}
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if not (200 <= resp.status < 300):
            message: str
            try:
                parsed = json.loads(data.decode("utf-8"))
                message = parsed.get("error", data.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                message = data.decode("utf-8", errors="replace")
            raise ControlClientError(resp.status, message)
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return data.decode("utf-8", errors="replace")
    finally:
        conn.close()


def get(path: str, **kw) -> Any:
    return request("GET", path, **kw)


def post(path: str, body: Any = None, **kw) -> Any:
    return request("POST", path, body=body, **kw)


def put(path: str, body: Any = None, **kw) -> Any:
    return request("PUT", path, body=body, **kw)


def delete(path: str, **kw) -> Any:
    return request("DELETE", path, **kw)
