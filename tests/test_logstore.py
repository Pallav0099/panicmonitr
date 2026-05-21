import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src import IST
from src.logstore import (
    BUCKET_1HOUR_SECS,
    BUCKET_5MIN_SECS,
    EV_CONTAINER_EXITED,
    LogStore,
)


def epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class LogStoreTests(unittest.TestCase):
    def make_store(self, tmp: str) -> LogStore:
        return LogStore(Path(tmp) / "logstore.db", own_node_id="node-a")

    def test_roll_up_5min_skips_already_bucketed_raw_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = self.make_store(d)
            try:
                now_epoch = epoch(datetime.now(IST))
                base = ((now_epoch // BUCKET_5MIN_SECS) - 4) * BUCKET_5MIN_SECS
                with store._lock:
                    store._conn.executemany(
                        "INSERT INTO raw_snapshots (peer_node_id, ts, payload) VALUES (?, ?, ?)",
                        [
                            ("node-a", base + 10, json.dumps({"cpu_percent": 10})),
                            ("node-a", base + BUCKET_5MIN_SECS + 10, json.dumps({"cpu_percent": 20})),
                        ],
                    )

                self.assertEqual(store.roll_up_5min(), 2)

                calls: list[int] = []
                original = store._aggregate_snaps

                def wrapped(snaps, window_start, window_end, bucket_size):
                    calls.append(window_start)
                    return original(snaps, window_start, window_end, bucket_size)

                store._aggregate_snaps = wrapped  # type: ignore[method-assign]
                with store._lock:
                    store._conn.execute(
                        "INSERT INTO raw_snapshots (peer_node_id, ts, payload) VALUES (?, ?, ?)",
                        ("node-a", base + 20, json.dumps({"cpu_percent": 99})),
                    )

                self.assertEqual(store.roll_up_5min(), 0)
                self.assertEqual(calls, [])
            finally:
                store.close()

    def test_merge_sync_payload_is_idempotent_for_events_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = self.make_store(d)
            try:
                ts = datetime.now(IST).replace(microsecond=0).isoformat()
                payload = {
                    "sync_strategy": "raw",
                    "raw_snapshots": [{"timestamp": ts, "cpu_percent": 11}],
                    "events": [{"ts": ts, "event": "container_exited", "detail": {"name": "api"}}],
                }

                self.assertEqual(
                    store.merge_sync_payload("peer-a", payload),
                    {"snapshots_merged": 1, "events_merged": 1, "strategy": "raw"},
                )
                self.assertEqual(
                    store.merge_sync_payload("peer-a", payload),
                    {"snapshots_merged": 0, "events_merged": 0, "strategy": "raw"},
                )
            finally:
                store.close()

    def test_roll_daily_counts_incidents_before_write_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = self.make_store(d)
            try:
                old = datetime.now(IST) - timedelta(days=8)
                hour_start = (epoch(old) // BUCKET_1HOUR_SECS) * BUCKET_1HOUR_SECS
                with store._lock:
                    store._conn.execute(
                        """INSERT INTO stat_buckets
                           (peer_node_id, window_start, window_end, bucket_size,
                            cpu_avg, mem_avg, samples)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "node-a",
                            hour_start,
                            hour_start + BUCKET_1HOUR_SECS,
                            BUCKET_1HOUR_SECS,
                            10.0,
                            20.0,
                            1,
                        ),
                    )

                original = store.get_events

                def checked_get_events(*args, **kwargs):
                    self.assertFalse(store._conn.in_transaction)
                    return original(*args, **kwargs)

                store.get_events = checked_get_events  # type: ignore[method-assign]
                store.record_event(EV_CONTAINER_EXITED, {"name": "api"})
                self.assertEqual(store.roll_daily(), 1)
            finally:
                store.close()
