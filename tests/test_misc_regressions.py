import io
import os
import stat
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import nacl.encoding
import nacl.signing

from src.controlsock import _ControlHandler
from src.identity import _atomic_write_bytes, _atomic_write_text
from src.log import OP_GENESIS, TrustLog
from src.notifier import WebhookNotifier
from src.webapp import WebApp, _HTML


class _ControlEngine:
    def __init__(self) -> None:
        self.removed: tuple[str, str] | None = None

    def remove_peer_tag(self, nid: str, tag: str):
        self.removed = (nid, tag)
        return None


class _FakeRole:
    value = "both"


class _FakeEngine:
    node_id = "a" * 64
    role = _FakeRole()
    stats_collector = None
    loop = None

    def get_own_stats(self):
        return None

    def get_device_states(self):
        return []

    @property
    def history(self):
        return None

    @property
    def logstore(self):
        return None


def node_id_for(key: nacl.signing.SigningKey) -> str:
    return key.verify_key.encode(encoder=nacl.encoding.HexEncoder).decode()


class MiscRegressionTests(unittest.TestCase):
    def test_controlsocket_delete_tag_uses_query_parser(self) -> None:
        engine = _ControlEngine()
        old_engine = getattr(_ControlHandler, "engine", None)
        _ControlHandler.engine = engine
        try:
            handler = _ControlHandler.__new__(_ControlHandler)
            handler.path = "/v1/peers/peer-a/tags?tag=foo&other=bar"
            handler._read_body = lambda: self.fail("_read_body should not be called")
            handler._reply = lambda err: setattr(handler, "reply_error", err)

            handler._route_peers("DELETE", ["peer-a", "tags"])

            self.assertEqual(engine.removed, ("peer-a", "foo"))
            self.assertIsNone(getattr(handler, "reply_error", None))
        finally:
            if old_engine is None:
                delattr(_ControlHandler, "engine")
            else:
                _ControlHandler.engine = old_engine

    def test_log_reload_does_not_clear_entries_before_parse(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            signing_key = nacl.signing.SigningKey.generate()
            own_node_id = node_id_for(signing_key)
            log = TrustLog(Path(d) / "log.jsonl", signing_key=signing_key, own_node_id=own_node_id)
            log.append(OP_GENESIS, {"node_id": own_node_id})
            self.assertEqual(len(log.entries()), 1)

            original = log._read_verified_entries

            def checked_read():
                self.assertEqual(len(log.entries()), 1)
                return original()

            log._read_verified_entries = checked_read  # type: ignore[method-assign]
            future = time.time() + 5
            os.utime(log._path, (future, future))

            self.assertTrue(log.reload_if_changed())
            self.assertEqual(len(log.entries()), 1)

    def test_identity_atomic_writes_create_restrictive_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "secret.key"
            real_replace = os.replace
            seen_modes: list[int] = []

            def checked_replace(src, dst):
                seen_modes.append(stat.S_IMODE(Path(src).stat().st_mode))
                real_replace(src, dst)

            with mock.patch("src.identity.os.replace", checked_replace):
                _atomic_write_bytes(path, b"secret", mode=0o600)
                _atomic_write_text(Path(d) / "meta.json", "{}", mode=0o600)

            self.assertEqual(seen_modes, [0o600, 0o600])
            self.assertEqual(path.read_bytes(), b"secret")

    def test_webhook_http_error_reads_available_body_safely(self) -> None:
        body = b"x" * 600
        err = urllib.error.HTTPError(
            "https://example.invalid",
            500,
            "server error",
            hdrs=None,
            fp=io.BytesIO(body),
        )

        with mock.patch("urllib.request.urlopen", side_effect=err):
            status_code, text = WebhookNotifier("https://example.invalid")._post(b"{}")

        self.assertEqual(status_code, 500)
        self.assertEqual(text, "x" * 512)

    def test_web_container_log_routes_reject_invalid_ids(self) -> None:
        app = WebApp(_FakeEngine(), port=0)
        class _Server:
            def serve_forever(self):
                return None

            def shutdown(self):
                return None

        with mock.patch("werkzeug.serving.make_server", return_value=_Server()):
            app.start()
        try:
            client = app._app.test_client()
            local = client.get("/api/container/bad;id/logs")
            peer = client.get(f"/api/node/{'b' * 64}/container/bad;id/logs")
            self.assertEqual(local.status_code, 400)
            self.assertEqual(peer.status_code, 400)
        finally:
            app.stop()

    def test_web_js_uses_abort_controller_and_stale_response_guards(self) -> None:
        self.assertIn("new AbortController()", _HTML)
        self.assertIn("state.token !== token", _HTML)
        self.assertIn("getNodeId(d)", _HTML)
