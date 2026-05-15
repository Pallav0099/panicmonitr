from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from src import IST
from src.schema import PeerStatus

DEFAULT_HISTORY_PATH = Path("./history.db")
DEFAULT_RETAIN_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    node_id   TEXT    NOT NULL,
    ts        INTEGER NOT NULL,   -- unix epoch seconds
    rtt_ms    REAL,               -- null on DEAD
    status    TEXT    NOT NULL    -- 'ALIVE' | 'DEAD'
);
CREATE INDEX IF NOT EXISTS idx_probes_node_ts ON probes(node_id, ts);
CREATE INDEX IF NOT EXISTS idx_probes_ts      ON probes(ts);
"""


@dataclass(slots=True)
class ProbeRow:
    node_id: str
    ts: datetime
    rtt_ms: float | None
    status: PeerStatus


def _to_epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _from_epoch(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=IST)


class HistoryStore:
    """SQLite-backed time-series of probe results.

    One row per probe. The heartbeat cycle inserts a row after every probe;
    ``uptime_percent`` and ``recent_rows`` are queried over the stored data.
    ``prune_older_than`` enforces rolling retention.

    Thread-safe via a per-instance lock around the connection. APScheduler
    fires the cycle from the event loop thread; the retention GC job runs on
    the same loop but we keep the lock anyway so read-only queries from the
    TUI's refresh timer can't interleave mid-write.
    """

    def __init__(
        self,
        path: Path = DEFAULT_HISTORY_PATH,
        retain_days: int = DEFAULT_RETAIN_DAYS,
    ) -> None:
        self._path = path
        self._retain_days = retain_days
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we batch via explicit txns when needed
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(_SCHEMA)
        logger.debug(
            "[history] opened {} retain_days={}", self._path, self._retain_days
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 S110
                pass  # best-effort cleanup on close

    def checkpoint(self) -> None:
        """Force a WAL checkpoint to reclaim disk space."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:  # noqa: BLE001 S110
                pass  # WAL checkpoint is best-effort

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record(
        self,
        node_id: str,
        ts: datetime,
        rtt_ms: float | None,
        status: PeerStatus,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO probes (node_id, ts, rtt_ms, status) VALUES (?, ?, ?, ?)",
                (node_id, _to_epoch(ts), rtt_ms, status.value),
            )

    def prune_older_than(self, cutoff: datetime | None = None) -> int:
        """Delete rows older than *cutoff*. Returns rows deleted."""
        if cutoff is None:
            cutoff = datetime.now(IST) - timedelta(days=self._retain_days)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM probes WHERE ts < ?", (_to_epoch(cutoff),)
            )
            deleted = cur.rowcount or 0
        if deleted:
            logger.debug("[history] pruned {} rows older than {}", deleted, cutoff)
        return deleted

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def uptime_percent(
        self,
        node_id: str,
        window: timedelta,
        now: datetime | None = None,
    ) -> float | None:
        """Return the percentage of probes in [now-window, now] that were ALIVE.

        Returns ``None`` if no probes fell in the window.
        """
        now = now or datetime.now(IST)
        cutoff = now - window
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'ALIVE' THEN 1 ELSE 0 END) AS ok,
                    COUNT(*) AS total
                FROM probes
                WHERE node_id = ? AND ts >= ? AND ts <= ?
                """,
                (node_id, _to_epoch(cutoff), _to_epoch(now)),
            )
            ok, total = cur.fetchone() or (0, 0)
        if not total:
            return None
        return 100.0 * (ok or 0) / total

    def recent_rows(
        self,
        node_id: str,
        hours: int = 24,
        limit: int | None = None,
    ) -> list[ProbeRow]:
        """Return probes for *node_id* within the last *hours*, oldest first."""
        cutoff = datetime.now(IST) - timedelta(hours=hours)
        sql = (
            "SELECT node_id, ts, rtt_ms, status FROM probes "
            "WHERE node_id = ? AND ts >= ? ORDER BY ts ASC"
        )
        params: tuple = (node_id, _to_epoch(cutoff))
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
        return [
            ProbeRow(
                node_id=r[0],
                ts=_from_epoch(r[1]),
                rtt_ms=r[2],
                status=PeerStatus(r[3]),
            )
            for r in rows
        ]

    def latest(self, node_id: str) -> ProbeRow | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT node_id, ts, rtt_ms, status FROM probes "
                "WHERE node_id = ? ORDER BY ts DESC LIMIT 1",
                (node_id,),
            )
            r = cur.fetchone()
        if r is None:
            return None
        return ProbeRow(
            node_id=r[0],
            ts=_from_epoch(r[1]),
            rtt_ms=r[2],
            status=PeerStatus(r[3]),
        )

    def count(self, node_id: str | None = None) -> int:
        with self._lock:
            if node_id is None:
                cur = self._conn.execute("SELECT COUNT(*) FROM probes")
            else:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM probes WHERE node_id = ?", (node_id,)
                )
            return cur.fetchone()[0]

    def count_in_window(
        self, hours: int = 24, node_id: str | None = None
    ) -> int:
        cutoff = datetime.now(IST) - timedelta(hours=hours)
        with self._lock:
            if node_id is None:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM probes WHERE ts >= ?",
                    (_to_epoch(cutoff),),
                )
            else:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM probes WHERE node_id = ? AND ts >= ?",
                    (node_id, _to_epoch(cutoff)),
                )
            return cur.fetchone()[0]

    def hourly_uptime_buckets(
        self, node_id: str, hours: int = 24
    ) -> list[float | None]:
        """Return uptime % for each of the last *hours* hours, oldest→newest.

        ``None`` means no probes fell in that bucket.
        """
        now = datetime.now(IST)
        start = now - timedelta(hours=hours)
        buckets: list[float | None] = [None] * hours
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    CAST((? - ts) / 3600 AS INTEGER) AS age_h,
                    SUM(CASE WHEN status = 'ALIVE' THEN 1 ELSE 0 END) AS ok,
                    COUNT(*) AS total
                FROM probes
                WHERE node_id = ? AND ts >= ? AND ts <= ?
                GROUP BY age_h
                """,
                (_to_epoch(now), node_id, _to_epoch(start), _to_epoch(now)),
            )
            for age_h, ok, total in cur.fetchall():
                idx = hours - 1 - int(age_h)
                if 0 <= idx < hours and total:
                    buckets[idx] = 100.0 * (ok or 0) / total
        return buckets

    def rtt_stats(self, node_id: str, hours: int = 24) -> dict:
        """Return {probes, alive, dead, rtt_min, rtt_max, rtt_avg} over *hours*.

        rtt_* are over ALIVE probes only; ``None`` if no such probes.
        """
        cutoff = datetime.now(IST) - timedelta(hours=hours)
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS probes,
                    SUM(CASE WHEN status = 'ALIVE' THEN 1 ELSE 0 END) AS alive,
                    MIN(CASE WHEN status = 'ALIVE' THEN rtt_ms END) AS rtt_min,
                    MAX(CASE WHEN status = 'ALIVE' THEN rtt_ms END) AS rtt_max,
                    AVG(CASE WHEN status = 'ALIVE' THEN rtt_ms END) AS rtt_avg
                FROM probes
                WHERE node_id = ? AND ts >= ?
                """,
                (node_id, _to_epoch(cutoff)),
            )
            row = cur.fetchone()
        probes = row[0] or 0
        alive = row[1] or 0
        return {
            "probes": probes,
            "alive": alive,
            "dead": max(0, probes - alive),
            "rtt_min": row[2],
            "rtt_max": row[3],
            "rtt_avg": row[4],
        }


# ---------------------------------------------------------------------------
# Convenience: parse window strings used by --uptime --window WINDOW
# ---------------------------------------------------------------------------

_WINDOW_ALIASES: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def parse_window(value: str) -> timedelta:
    v = value.strip().lower()
    if v in _WINDOW_ALIASES:
        return _WINDOW_ALIASES[v]
    raise ValueError(
        f"unsupported window '{value}' (use one of: {', '.join(_WINDOW_ALIASES)})"
    )


DEFAULT_WINDOWS: list[tuple[str, timedelta]] = [
    ("1h", _WINDOW_ALIASES["1h"]),
    ("24h", _WINDOW_ALIASES["24h"]),
    ("7d", _WINDOW_ALIASES["7d"]),
    ("30d", _WINDOW_ALIASES["30d"]),
]
