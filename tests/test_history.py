import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from src import IST
from src.history import HistoryStore
from src.schema import PeerStatus


class FixedDateTime(datetime):
    fixed_now: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.fixed_now if tz is None else cls.fixed_now.astimezone(tz)


class HistoryStoreTests(unittest.TestCase):
    def test_record_many_batches_rows_and_counts_them(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = HistoryStore(Path(d) / "history.db")
            try:
                now = datetime.now(IST)
                store.record_many(
                    [
                        ("peer-a", now, 1.0, PeerStatus.ALIVE),
                        ("peer-b", now, None, PeerStatus.DEAD),
                    ]
                )
                self.assertEqual(store.count(), 2)
            finally:
                store.close()

    def test_hourly_uptime_buckets_use_epoch_hour_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = HistoryStore(Path(d) / "history.db")
            try:
                fixed = datetime(2026, 5, 22, 10, 30, tzinfo=IST)
                current_hour = int(fixed.timestamp()) // 3600 * 3600
                current_boundary = datetime.fromtimestamp(current_hour, tz=IST)
                previous_hour = datetime.fromtimestamp(current_hour - 1, tz=IST)
                store.record_many(
                    [
                        ("peer-a", previous_hour, None, PeerStatus.DEAD),
                        ("peer-a", current_boundary, 5.0, PeerStatus.ALIVE),
                    ]
                )

                FixedDateTime.fixed_now = fixed
                with mock.patch("src.history.datetime", FixedDateTime):
                    self.assertEqual(
                        store.hourly_uptime_buckets("peer-a", hours=2),
                        [0.0, 100.0],
                    )
            finally:
                store.close()

