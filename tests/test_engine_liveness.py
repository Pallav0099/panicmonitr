import unittest
from datetime import datetime
from unittest import mock

from src import IST
from src.engine import MonitorEngine
from src.log import OP_MONITOR_DOWN
from src.schema import LatencyRecord, PeerEntry, PeerState, PeerStatus


class _Trust:
    def get_peer(self, node_id: str):
        return None


class _Log:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def append(self, type_: str, data: dict) -> None:
        self.entries.append((type_, data))


class _Notifier:
    def __init__(self) -> None:
        self.events = []

    async def notify(self, event) -> None:
        self.events.append(event)


class _History:
    def __init__(self) -> None:
        self.calls = []

    def record_many(self, rows) -> None:
        self.calls.append(list(rows))


def minimal_engine() -> MonitorEngine:
    engine = MonitorEngine.__new__(MonitorEngine)
    engine._down_after = 2
    engine._up_after = 1
    engine._flap_dwell_seconds = 0
    engine._last_alert_fired = {}
    engine._trust = _Trust()
    engine._log = _Log()
    engine._notifier = _Notifier()
    engine._node_id_str = "f" * 64
    engine._state_locks = {}
    engine._spawn_bg = lambda coro: None
    return engine


class EngineLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_failures_do_not_emit_monitor_down(self) -> None:
        engine = minimal_engine()
        peer = PeerState(PeerEntry(node_id="a" * 64, alias="peer-a"))
        peer.current_status = PeerStatus.UNKNOWN
        peer.consecutive_failures = 2
        record = LatencyRecord(timestamp=datetime.now(IST), rtt_ms=None, status=PeerStatus.DEAD)

        await engine._maybe_transition(peer, record, datetime.now(IST))

        self.assertEqual(peer.current_status, PeerStatus.UNKNOWN)
        self.assertEqual(engine._log.entries, [])

    async def test_alive_failures_emit_monitor_down_after_threshold(self) -> None:
        engine = minimal_engine()
        peer = PeerState(PeerEntry(node_id="a" * 64, alias="peer-a"))
        peer.current_status = PeerStatus.ALIVE
        peer.consecutive_failures = 2
        peer.last_fail_reason = "timeout"
        record = LatencyRecord(timestamp=datetime.now(IST), rtt_ms=None, status=PeerStatus.DEAD)

        await engine._maybe_transition(peer, record, datetime.now(IST))

        self.assertEqual(peer.current_status, PeerStatus.DEAD)
        self.assertEqual(engine._log.entries[0][0], OP_MONITOR_DOWN)

    async def test_heartbeat_cycle_batches_probe_history_writes(self) -> None:
        engine = minimal_engine()
        engine._iroh = object()
        engine._check_reload = lambda: None
        engine._history = _History()
        peers = [
            PeerState(PeerEntry(node_id="a" * 64, alias="a")),
            PeerState(PeerEntry(node_id="b" * 64, alias="b")),
        ]
        engine._devices = {p.entry.node_id: p for p in peers}

        async def fake_probe(peer):
            return LatencyRecord(timestamp=datetime.now(IST), rtt_ms=1.0, status=PeerStatus.ALIVE)

        engine._probe_peer = fake_probe

        async def inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with mock.patch("src.engine.asyncio.to_thread", inline_to_thread):
            await MonitorEngine._run_heartbeat_cycle(engine)

        self.assertEqual(len(engine._history.calls), 1)
        self.assertEqual(len(engine._history.calls[0]), 2)
