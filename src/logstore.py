"""src/logstore.py — Server-side log store for the monitored node (Phase 2).

Stores:
  • event_log  — state transitions only (agent start/stop, container events)
  • raw_snapshots — last 2 hours, ~10s interval ring buffer
  • stat_buckets  — rolled-up aggregates (5-min, 1-hour)
  • daily_summaries — per-day summaries

Rollup schedule (driven by APScheduler in engine.py):
  every 5 min  → raw → 5-min buckets
  every hour   → 5-min (>24h old) → 1-hour buckets
  every day    → 1-hour (>7d old) → daily summaries
  every hour   → prune raw (>2h), prune 5-min (>30d)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

from src import IST, paths

DEFAULT_LOGSTORE_PATH = paths.default_logstore_path()

# Retention config
RAW_RETAIN_HOURS = 2
BUCKET_5MIN_RETAIN_DAYS = 30
# 1-hour and daily summaries are kept forever

BUCKET_5MIN_SECS = 300
BUCKET_1HOUR_SECS = 3600

# Event type constants
EV_AGENT_STARTED = "agent_started"
EV_AGENT_SHUTDOWN = "agent_shutdown"
EV_CONTAINER_STARTED = "container_started"
EV_CONTAINER_EXITED = "container_exited"
EV_CONTAINER_RESTARTED = "container_restarted"
EV_CONTAINER_UNHEALTHY = "container_unhealthy"
EV_SYSTEM_HIGH_CPU = "system_high_cpu"
EV_SYSTEM_HIGH_MEM = "system_high_mem"
EV_DISK_NEAR_FULL = "disk_near_full"

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_node_id TEXT    NOT NULL DEFAULT '',
    ts           INTEGER NOT NULL,
    event        TEXT    NOT NULL,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_peer_ts ON events(peer_node_id, ts);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    peer_node_id TEXT    NOT NULL DEFAULT '',
    ts           INTEGER NOT NULL,
    payload      TEXT    NOT NULL,
    PRIMARY KEY (peer_node_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_snapshots(ts);

CREATE TABLE IF NOT EXISTS stat_buckets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_node_id TEXT    NOT NULL DEFAULT '',
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    bucket_size  INTEGER NOT NULL,
    cpu_avg      REAL,
    cpu_max      REAL,
    mem_avg      REAL,
    mem_max      REAL,
    disk_pct_avg REAL,
    net_rx_delta INTEGER,
    net_tx_delta INTEGER,
    samples      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_buckets_window
    ON stat_buckets(peer_node_id, window_start, bucket_size);

CREATE TABLE IF NOT EXISTS daily_summaries (
    peer_node_id TEXT NOT NULL DEFAULT '',
    date         TEXT NOT NULL,
    uptime_pct   REAL,
    avg_cpu      REAL,
    avg_mem      REAL,
    incidents    INTEGER DEFAULT 0,
    PRIMARY KEY (peer_node_id, date)
);
"""


@dataclass
class EventRow:
    id: int
    ts: datetime
    event: str
    detail: Optional[dict]


@dataclass
class StatBucket:
    window_start: datetime
    window_end: datetime
    bucket_size: int   # seconds
    cpu_avg: Optional[float]
    cpu_max: Optional[float]
    mem_avg: Optional[float]
    mem_max: Optional[float]
    disk_pct_avg: Optional[float]
    net_rx_delta: Optional[int]
    net_tx_delta: Optional[int]
    samples: int

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "bucket_size": self.bucket_size,
            "cpu_avg": self.cpu_avg,
            "cpu_max": self.cpu_max,
            "mem_avg": self.mem_avg,
            "mem_max": self.mem_max,
            "disk_pct_avg": self.disk_pct_avg,
            "net_rx_delta": self.net_rx_delta,
            "net_tx_delta": self.net_tx_delta,
            "samples": self.samples,
        }


def _to_epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _from_epoch(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=IST)


class LogStore:
    """Server-side persistent log store for the monitored node.

    Thread-safe via a per-instance lock.
    """

    def __init__(
        self,
        path: Path = DEFAULT_LOGSTORE_PATH,
        own_node_id: str = "",
    ) -> None:
        self._path = path
        self._own_node_id = own_node_id
        # RLock so transactional methods that call into other lock-acquiring
        # helpers (e.g. roll_daily → get_events) don't deadlock on themselves.
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        # Migration first: any legacy tables get peer_node_id added BEFORE the
        # CREATE INDEX statements in _SCHEMA reference that column.
        self._migrate_peer_node_id_column()
        self._conn.executescript(_SCHEMA)
        self._migrate_event_timestamps()
        self._migrate_event_dedupe()
        self._migrate_snapshot_seq()
        logger.info("[logstore] opened {}", self._path)

    def _has_column(self, table: str, column: str) -> bool:
        cur = self._conn.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column for row in cur.fetchall())

    def _table_exists(self, table: str) -> bool:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        )
        return cur.fetchone() is not None

    def _migrate_peer_node_id_column(self) -> None:
        """One-shot: add peer_node_id to legacy schemas and backfill with own_node_id.

        Pre-existing databases were created with peer_node_id absent. For
        tables where the column can simply be appended (events, stat_buckets)
        we ALTER + UPDATE. For tables whose primary key changes
        (raw_snapshots, daily_summaries) we rebuild via a temporary table.
        """
        own = self._own_node_id or "self"
        migrations: list[str] = []

        # --- events: append column if missing -----------------------------
        if self._table_exists("events") and not self._has_column("events", "peer_node_id"):
            migrations += [
                "ALTER TABLE events ADD COLUMN peer_node_id TEXT NOT NULL DEFAULT ''",
                f"UPDATE events SET peer_node_id = '{own}' WHERE peer_node_id = ''",
            ]

        # --- stat_buckets: append column if missing -----------------------
        if self._table_exists("stat_buckets") and not self._has_column("stat_buckets", "peer_node_id"):
            migrations += [
                "ALTER TABLE stat_buckets ADD COLUMN peer_node_id TEXT NOT NULL DEFAULT ''",
                f"UPDATE stat_buckets SET peer_node_id = '{own}' WHERE peer_node_id = ''",
                "DROP INDEX IF EXISTS idx_buckets_window",
            ]

        # --- raw_snapshots: PK changes (ts -> (peer_node_id, ts)) ---------
        if self._table_exists("raw_snapshots") and not self._has_column("raw_snapshots", "peer_node_id"):
            migrations += [
                "ALTER TABLE raw_snapshots RENAME TO raw_snapshots_legacy",
                """CREATE TABLE raw_snapshots (
                    peer_node_id TEXT    NOT NULL DEFAULT '',
                    ts           INTEGER NOT NULL,
                    payload      TEXT    NOT NULL,
                    PRIMARY KEY (peer_node_id, ts)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_snapshots(ts)",
                f"""INSERT INTO raw_snapshots (peer_node_id, ts, payload)
                    SELECT '{own}', ts, payload FROM raw_snapshots_legacy""",
                "DROP TABLE raw_snapshots_legacy",
            ]

        # --- daily_summaries: PK changes (date -> (peer_node_id, date)) ---
        if self._table_exists("daily_summaries") and not self._has_column("daily_summaries", "peer_node_id"):
            migrations += [
                "ALTER TABLE daily_summaries RENAME TO daily_summaries_legacy",
                """CREATE TABLE daily_summaries (
                    peer_node_id TEXT NOT NULL DEFAULT '',
                    date         TEXT NOT NULL,
                    uptime_pct   REAL,
                    avg_cpu      REAL,
                    avg_mem      REAL,
                    incidents    INTEGER DEFAULT 0,
                    PRIMARY KEY (peer_node_id, date)
                )""",
                f"""INSERT INTO daily_summaries
                        (peer_node_id, date, uptime_pct, avg_cpu, avg_mem, incidents)
                    SELECT '{own}', date, uptime_pct, avg_cpu, avg_mem, incidents
                    FROM daily_summaries_legacy""",
                "DROP TABLE daily_summaries_legacy",
            ]

        if not migrations:
            return
        with self._lock:
            for stmt in migrations:
                self._conn.execute(stmt)
        logger.info("[logstore] migrated schema (added peer_node_id column)")

    def _migrate_event_dedupe(self) -> None:
        """Deduplicate legacy events before installing the unique index."""
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM events
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM events
                    GROUP BY peer_node_id, ts, event, COALESCE(detail, '')
                )
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe
                ON events(peer_node_id, ts, event, COALESCE(detail, ''))
                """
            )

    def _migrate_event_timestamps(self) -> None:
        """Convert legacy second-precision event timestamps to milliseconds.

        Detects old-format timestamps (ts < 1e11) and multiplies them to
        match the new ms-precision convention.  Idempotent — a second
        run is a no-op.
        """
        if not self._table_exists("events"):
            return
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE ts > 0 AND ts < 100000000000"
            )
            stale = cur.fetchone()[0]
            if stale > 0:
                self._conn.execute(
                    "UPDATE events SET ts = ts * 1000 WHERE ts > 0 AND ts < 100000000000"
                )
                logger.info(
                    "[logstore] migrated {} event timestamps to ms precision", stale
                )

    def _migrate_snapshot_seq(self) -> None:
        """Add monotonic ``seq`` column to raw_snapshots for delta pulls."""
        if not self._table_exists("raw_snapshots"):
            return
        if self._has_column("raw_snapshots", "seq"):
            return
        with self._lock:
            self._conn.execute("ALTER TABLE raw_snapshots ADD COLUMN seq INTEGER")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_raw_peer_seq "
                "ON raw_snapshots(peer_node_id, seq)"
            )
            own = self._own_node_id or "self"
            self._conn.execute(
                "UPDATE raw_snapshots SET seq = rowid WHERE peer_node_id = ? AND seq IS NULL",
                (own,),
            )
            logger.info("[logstore] migrated raw_snapshots: added seq column")

    def _next_seq(self, peer_node_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM raw_snapshots WHERE peer_node_id = ?",
            (peer_node_id,),
        )
        return cur.fetchone()[0]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def record_event(self, event: str, detail: Optional[dict] = None) -> None:
        ts = int(datetime.now(IST).timestamp() * 1000)
        payload = json.dumps(detail) if detail else None
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO events (peer_node_id, ts, event, detail) VALUES (?, ?, ?, ?)",
                (self._own_node_id or "self", ts, event, payload),
            )
        logger.debug("[logstore] event: {} {}", event, detail or "")

    def get_events(
        self,
        since_ts: Optional[datetime] = None,
        until_ts: Optional[datetime] = None,
        limit: int = 1000,
        peer_node_id: Optional[str] = None,
    ) -> list[EventRow]:
        since = int(_to_epoch(since_ts) * 1000) if since_ts else 0
        until = int(_to_epoch(until_ts) * 1000) if until_ts else 9_999_999_999_999
        nid = peer_node_id if peer_node_id is not None else (self._own_node_id or "self")
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ts, event, detail FROM events "
                "WHERE peer_node_id = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT ?",
                (nid, since, until, limit),
            )
            rows = cur.fetchall()
        return [
            EventRow(
                id=r[0],
                ts=datetime.fromtimestamp(r[1] / 1000.0, tz=IST),
                event=r[2],
                detail=json.loads(r[3]) if r[3] else None,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Raw snapshots
    # ------------------------------------------------------------------

    def record_snapshot(self, snap_dict: dict) -> int:
        """Insert a raw snapshot and return its sequence number.

        Pruning is handled by the hourly ``prune()`` job.
        """
        ts = _to_epoch(datetime.now(IST))
        payload = json.dumps(snap_dict)
        own = self._own_node_id or "self"
        with self._lock:
            seq = self._next_seq(own)
            self._conn.execute(
                "INSERT OR REPLACE INTO raw_snapshots (peer_node_id, ts, payload, seq) "
                "VALUES (?, ?, ?, ?)",
                (own, ts, payload, seq),
            )
            return seq

    def get_raw_snapshots(
        self,
        since_ts: Optional[datetime] = None,
        until_ts: Optional[datetime] = None,
        peer_node_id: Optional[str] = None,
    ) -> list[dict]:
        since = _to_epoch(since_ts) if since_ts else 0
        until = _to_epoch(until_ts) if until_ts else 9_999_999_999
        nid = peer_node_id if peer_node_id is not None else (self._own_node_id or "self")
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM raw_snapshots "
                "WHERE peer_node_id = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
                (nid, since, until),
            )
            rows = cur.fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_delta_since_seq(self, since_seq: int, peer_node_id: Optional[str] = None) -> dict:
        """Return lightweight snapshot entries with seq > since_seq.

        Strips ``processes`` and ``containers`` from history entries to keep
        the payload small.  The caller includes the full latest snapshot
        separately as ``own_stats``.
        """
        nid = peer_node_id if peer_node_id is not None else (self._own_node_id or "self")
        with self._lock:
            cur = self._conn.execute(
                "SELECT seq, payload FROM raw_snapshots "
                "WHERE peer_node_id = ? AND seq > ? ORDER BY seq ASC",
                (nid, since_seq),
            )
            rows = cur.fetchall()
            cur2 = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM raw_snapshots WHERE peer_node_id = ?",
                (nid,),
            )
            latest_seq = cur2.fetchone()[0]

        entries = []
        for seq_val, raw in rows:
            snap = json.loads(raw)
            entries.append({
                "seq": seq_val,
                "timestamp": snap.get("timestamp"),
                "cpu_percent": snap.get("cpu_percent"),
                "mem_percent": snap.get("mem_percent"),
                "disk_percent": snap.get("disk_percent"),
            })
        return {"latest_seq": latest_seq, "entries": entries}

    # ------------------------------------------------------------------
    # Rollup jobs
    # ------------------------------------------------------------------

    def roll_up_5min(self) -> int:
        """Aggregate raw snapshots older than 0s into 5-minute buckets.

        Returns number of new buckets created.
        """
        now = datetime.now(IST)
        own = self._own_node_id or "self"
        now_epoch = _to_epoch(now)
        cutoff = now_epoch - (now_epoch % BUCKET_5MIN_SECS)
        with self._lock:
            cur = self._conn.execute(
                "SELECT MAX(window_end) FROM stat_buckets "
                "WHERE peer_node_id = ? AND bucket_size = ?",
                (own, BUCKET_5MIN_SECS),
            )
            row = cur.fetchone()
            latest_window_end = row[0] if row and row[0] is not None else None
            lower_bound = latest_window_end if latest_window_end is not None else 0
            cur = self._conn.execute(
                "SELECT ts, payload FROM raw_snapshots "
                "WHERE peer_node_id = ? AND ts >= ? AND ts < ? ORDER BY ts ASC",
                (own, lower_bound, cutoff),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        buckets: dict[int, list[dict]] = {}
        for ts_epoch, payload_str in rows:
            window_start = (ts_epoch // BUCKET_5MIN_SECS) * BUCKET_5MIN_SECS
            buckets.setdefault(window_start, []).append(json.loads(payload_str))

        # One transaction for the whole rollup. The previous per-bucket
        # acquire/release pattern issued an auto-commit per row, which on
        # backlog rollups translated to hundreds of fsync()s.
        created = 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for window_start, snaps in buckets.items():
                    window_end = window_start + BUCKET_5MIN_SECS
                    bucket = self._aggregate_snaps(
                        snaps, window_start, window_end, BUCKET_5MIN_SECS
                    )
                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO stat_buckets
                        (peer_node_id, window_start, window_end, bucket_size,
                         cpu_avg, cpu_max, mem_avg, mem_max,
                         disk_pct_avg, net_rx_delta, net_tx_delta, samples)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            own, window_start, window_end, BUCKET_5MIN_SECS,
                            bucket.cpu_avg, bucket.cpu_max,
                            bucket.mem_avg, bucket.mem_max,
                            bucket.disk_pct_avg,
                            bucket.net_rx_delta, bucket.net_tx_delta,
                            bucket.samples,
                        ),
                    )
                    if cur.rowcount > 0:
                        created += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return created

    def roll_hourly(self) -> int:
        """Aggregate 5-min buckets >24h old into 1-hour buckets."""
        now = datetime.now(IST)
        own = self._own_node_id or "self"
        cutoff = _to_epoch(now - timedelta(hours=24))
        with self._lock:
            cur = self._conn.execute(
                """SELECT window_start, window_end, cpu_avg, cpu_max,
                          mem_avg, mem_max, disk_pct_avg, net_rx_delta, net_tx_delta, samples
                   FROM stat_buckets
                   WHERE peer_node_id = ? AND bucket_size = ? AND window_start <= ?
                   ORDER BY window_start ASC""",
                (own, BUCKET_5MIN_SECS, cutoff),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        # Group by hour
        hour_buckets: dict[int, list] = {}
        for row in rows:
            window_start = row[0]
            hour_start = (window_start // BUCKET_1HOUR_SECS) * BUCKET_1HOUR_SECS
            hour_buckets.setdefault(hour_start, []).append(row)

        created = 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for hour_start, subbuckets in hour_buckets.items():
                    hour_end = hour_start + BUCKET_1HOUR_SECS
                    total_samples = sum(r[9] for r in subbuckets)
                    if total_samples == 0:
                        continue

                    cpu_avgs = [r[2] for r in subbuckets if r[2] is not None]
                    cpu_maxes = [r[3] for r in subbuckets if r[3] is not None]
                    mem_avgs = [r[4] for r in subbuckets if r[4] is not None]
                    mem_maxes = [r[5] for r in subbuckets if r[5] is not None]
                    disk_avgs = [r[6] for r in subbuckets if r[6] is not None]
                    net_rx = sum(r[7] for r in subbuckets if r[7] is not None)
                    net_tx = sum(r[8] for r in subbuckets if r[8] is not None)

                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO stat_buckets
                        (peer_node_id, window_start, window_end, bucket_size,
                         cpu_avg, cpu_max, mem_avg, mem_max,
                         disk_pct_avg, net_rx_delta, net_tx_delta, samples)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            own, hour_start, hour_end, BUCKET_1HOUR_SECS,
                            round(sum(cpu_avgs) / len(cpu_avgs), 2) if cpu_avgs else None,
                            max(cpu_maxes) if cpu_maxes else None,
                            round(sum(mem_avgs) / len(mem_avgs), 2) if mem_avgs else None,
                            max(mem_maxes) if mem_maxes else None,
                            round(sum(disk_avgs) / len(disk_avgs), 2) if disk_avgs else None,
                            net_rx, net_tx,
                            total_samples,
                        ),
                    )
                    if cur.rowcount > 0:
                        created += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return created

    def roll_daily(self) -> int:
        """Aggregate 1-hour buckets >7d old into daily summaries."""
        now = datetime.now(IST)
        own = self._own_node_id or "self"
        cutoff = _to_epoch(now - timedelta(days=7))
        with self._lock:
            cur = self._conn.execute(
                """SELECT window_start, cpu_avg, mem_avg, samples
                   FROM stat_buckets
                   WHERE peer_node_id = ? AND bucket_size = ? AND window_start <= ?
                   ORDER BY window_start ASC""",
                (own, BUCKET_1HOUR_SECS, cutoff),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        # Group by date
        day_buckets: dict[str, list] = {}
        for ws, cpu_avg, mem_avg, samples in rows:
            dt = _from_epoch(ws)
            date_key = dt.strftime("%Y-%m-%d")
            day_buckets.setdefault(date_key, []).append((cpu_avg, mem_avg, samples))

        incident_counts: dict[str, int] = {}
        for date_key in day_buckets:
            try:
                day_dt = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=IST)
                day_end = day_dt + timedelta(days=1)
                inc_events = self.get_events(
                    since_ts=day_dt,
                    until_ts=day_end,
                    peer_node_id=own,
                )
                incident_counts[date_key] = sum(
                    1
                    for e in inc_events
                    if e.event in (EV_CONTAINER_EXITED, EV_CONTAINER_UNHEALTHY)
                )
            except Exception:
                incident_counts[date_key] = 0

        created = 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for date_key, buckets in day_buckets.items():
                    cpus = [b[0] for b in buckets if b[0] is not None]
                    mems = [b[1] for b in buckets if b[1] is not None]
                    incidents = incident_counts.get(date_key, 0)

                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO daily_summaries
                           (peer_node_id, date, uptime_pct, avg_cpu, avg_mem, incidents)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            own,
                            date_key,
                            None,
                            round(sum(cpus) / len(cpus), 2) if cpus else None,
                            round(sum(mems) / len(mems), 2) if mems else None,
                            incidents,
                        ),
                    )
                    if cur.rowcount > 0:
                        created += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return created

    def prune(self) -> dict:
        """Enforce retention limits. Returns counts of deleted rows.

        Prunes across all peers, since the retention policy is the same for
        own data and mirrored-peer data.
        """
        now = datetime.now(IST)
        raw_cutoff = _to_epoch(now - timedelta(hours=RAW_RETAIN_HOURS))
        bucket_cutoff = _to_epoch(now - timedelta(days=BUCKET_5MIN_RETAIN_DAYS))

        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM raw_snapshots WHERE ts < ?", (raw_cutoff,)
            )
            raw_deleted = cur.rowcount or 0

            cur = self._conn.execute(
                "DELETE FROM stat_buckets WHERE bucket_size = ? AND window_start < ?",
                (BUCKET_5MIN_SECS, bucket_cutoff),
            )
            bucket_deleted = cur.rowcount or 0

            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

        return {"raw_deleted": raw_deleted, "buckets_deleted": bucket_deleted}

    # ------------------------------------------------------------------
    # Sync payload computation
    # ------------------------------------------------------------------

    def get_sync_payload(self, last_seen_ts: datetime) -> dict:
        """Compute and return sync payload appropriate for the gap duration.

        Gap decision tree (from the plan):
          < 1h  → raw snapshots
          1h-24h → 5-min buckets + events + last 30min raw
          24h-7d → 1-hour buckets + events
          > 7d  → daily summaries + events only
        """
        now = datetime.now(IST)
        gap_seconds = (now - last_seen_ts).total_seconds()

        events = self.get_events(since_ts=last_seen_ts)
        events_out = [
            {
                "ts": e.ts.isoformat(),
                "event": e.event,
                "detail": e.detail,
            }
            for e in events
        ]

        if gap_seconds < 3600:
            strategy = "raw"
            raw = self.get_raw_snapshots(since_ts=last_seen_ts)
            buckets: list[dict] = []
        elif gap_seconds < 86400:
            strategy = "5min"
            raw = self.get_raw_snapshots(since_ts=now - timedelta(minutes=30))
            buckets = self._get_buckets(
                since_ts=last_seen_ts,
                bucket_size=BUCKET_5MIN_SECS,
            )
        elif gap_seconds < 7 * 86400:
            strategy = "1hour"
            raw = []
            buckets = self._get_buckets(
                since_ts=last_seen_ts,
                bucket_size=BUCKET_1HOUR_SECS,
            )
        else:
            strategy = "daily"
            raw = []
            buckets = self._get_daily_summaries(since_ts=last_seen_ts)

        return {
            "gap_start": last_seen_ts.isoformat(),
            "gap_end": now.isoformat(),
            "gap_seconds": gap_seconds,
            "sync_strategy": strategy,
            "events": events_out,
            "buckets": buckets,
            "raw_snapshots": raw,
        }

    def _get_buckets(
        self,
        since_ts: datetime,
        bucket_size: int,
    ) -> list[dict]:
        since = _to_epoch(since_ts)
        own = self._own_node_id or "self"
        with self._lock:
            cur = self._conn.execute(
                """SELECT window_start, window_end, bucket_size,
                          cpu_avg, cpu_max, mem_avg, mem_max,
                          disk_pct_avg, net_rx_delta, net_tx_delta, samples
                   FROM stat_buckets
                   WHERE peer_node_id = ? AND bucket_size = ? AND window_start >= ?
                   ORDER BY window_start ASC""",
                (own, bucket_size, since),
            )
            rows = cur.fetchall()
        return [
            StatBucket(
                window_start=_from_epoch(r[0]),
                window_end=_from_epoch(r[1]),
                bucket_size=r[2],
                cpu_avg=r[3], cpu_max=r[4],
                mem_avg=r[5], mem_max=r[6],
                disk_pct_avg=r[7],
                net_rx_delta=r[8], net_tx_delta=r[9],
                samples=r[10],
            ).to_dict()
            for r in rows
        ]

    def _get_daily_summaries(self, since_ts: datetime) -> list[dict]:
        since_date = since_ts.strftime("%Y-%m-%d")
        own = self._own_node_id or "self"
        with self._lock:
            cur = self._conn.execute(
                "SELECT date, uptime_pct, avg_cpu, avg_mem, incidents "
                "FROM daily_summaries WHERE peer_node_id = ? AND date >= ? "
                "ORDER BY date ASC",
                (own, since_date,),
            )
            rows = cur.fetchall()
        return [
            {
                "date": r[0],
                "uptime_pct": r[1],
                "avg_cpu": r[2],
                "avg_mem": r[3],
                "incidents": r[4],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def latest_snapshot(self, peer_node_id: Optional[str] = None) -> Optional[dict]:
        nid = peer_node_id if peer_node_id is not None else (self._own_node_id or "self")
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM raw_snapshots "
                "WHERE peer_node_id = ? ORDER BY ts DESC LIMIT 1",
                (nid,),
            )
            row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def recent_snapshots(self, minutes: int = 60, peer_node_id: Optional[str] = None) -> list[dict]:
        nid = peer_node_id if peer_node_id is not None else (self._own_node_id or "self")
        cutoff = _to_epoch(datetime.now(IST) - timedelta(minutes=minutes))
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM raw_snapshots "
                "WHERE peer_node_id = ? AND ts >= ? ORDER BY ts ASC",
                (nid, cutoff),
            )
            rows = cur.fetchall()
        return [json.loads(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Peer-keyed methods (added in P3.5a for cross-device sync)
    # ------------------------------------------------------------------

    def last_seen_for_peer(self, peer_node_id: str) -> Optional[datetime]:
        """Return the most recent timestamp for which we have data on *peer*."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT MAX(ts) FROM (
                    SELECT MAX(ts) AS ts FROM raw_snapshots WHERE peer_node_id = ?
                    UNION ALL
                    SELECT MAX(ts) AS ts FROM events        WHERE peer_node_id = ?
                    UNION ALL
                    SELECT MAX(window_end) AS ts FROM stat_buckets
                        WHERE peer_node_id = ?
                )
                """,
                (peer_node_id, peer_node_id, peer_node_id),
            )
            row = cur.fetchone()
            max_epoch = row[0] if row and row[0] is not None else None

            cur = self._conn.execute(
                "SELECT MAX(date) FROM daily_summaries WHERE peer_node_id = ?",
                (peer_node_id,),
            )
            date_row = cur.fetchone()
            if date_row and date_row[0] is not None:
                try:
                    dt = datetime.strptime(date_row[0], "%Y-%m-%d").replace(tzinfo=IST)
                    date_epoch = _to_epoch(dt)
                    max_epoch = max(max_epoch or 0, date_epoch)
                except ValueError:
                    pass

        return _from_epoch(max_epoch) if max_epoch is not None else None

    def snapshots_for_peer(
        self,
        peer_node_id: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[dict]:
        return self.get_raw_snapshots(
            since_ts=since, until_ts=until, peer_node_id=peer_node_id
        )

    def recent_snapshots_for_peer(
        self, peer_node_id: str, hours: int = 1
    ) -> list[dict]:
        return self.recent_snapshots(minutes=hours * 60, peer_node_id=peer_node_id)

    def merge_sync_payload(self, peer_node_id: str, payload: dict) -> dict:
        """Idempotently insert a peer's sync payload into local tables.

        Returns ``{snapshots_merged, events_merged, buckets_merged, strategy}``.
        """
        snaps = payload.get("raw_snapshots") or []
        events = payload.get("events") or []
        buckets = payload.get("buckets") or []
        strategy = payload.get("sync_strategy") or "unknown"

        snapshots_merged = 0
        with self._lock:
            for snap in snaps:
                ts_raw = snap.get("ts") or snap.get("timestamp")
                if ts_raw is None:
                    continue
                try:
                    ts_epoch = (
                        ts_raw
                        if isinstance(ts_raw, (int, float))
                        else _to_epoch(datetime.fromisoformat(ts_raw))
                    )
                except (ValueError, TypeError):
                    continue
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO raw_snapshots (peer_node_id, ts, payload) VALUES (?, ?, ?)",
                    (peer_node_id, int(ts_epoch), json.dumps(snap)),
                )
                if cur.rowcount:
                    snapshots_merged += 1

            events_merged = 0
            for ev in events:
                ts_raw = ev.get("ts")
                if ts_raw is None:
                    continue
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts_epoch = ts_raw
                    else:
                        dt = datetime.fromisoformat(ts_raw)
                        ts_epoch = int(dt.timestamp() * 1000)
                    if ts_epoch < 100000000000:
                        ts_epoch = int(ts_epoch * 1000)
                except (ValueError, TypeError):
                    continue
                detail = ev.get("detail")
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO events (peer_node_id, ts, event, detail) VALUES (?, ?, ?, ?)",
                    (
                        peer_node_id,
                        int(ts_epoch),
                        ev.get("event") or "",
                        json.dumps(detail) if detail else None,
                    ),
                )
                if cur.rowcount:
                    events_merged += 1

            buckets_merged = 0
            for b in buckets:
                if "window_start" in b:
                    # StatBucket-style entry
                    ws = b.get("window_start")
                    we = b.get("window_end")
                    if ws is None or we is None:
                        continue
                    try:
                        ws_epoch = (
                            ws if isinstance(ws, (int, float))
                            else _to_epoch(datetime.fromisoformat(ws))
                        )
                        we_epoch = (
                            we if isinstance(we, (int, float))
                            else _to_epoch(datetime.fromisoformat(we))
                        )
                    except (ValueError, TypeError):
                        continue
                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO stat_buckets
                        (peer_node_id, window_start, window_end, bucket_size,
                         cpu_avg, cpu_max, mem_avg, mem_max,
                         disk_pct_avg, net_rx_delta, net_tx_delta, samples)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            peer_node_id,
                            int(ws_epoch), int(we_epoch),
                            b.get("bucket_size", 0),
                            b.get("cpu_avg"), b.get("cpu_max"),
                            b.get("mem_avg"), b.get("mem_max"),
                            b.get("disk_pct_avg"),
                            b.get("net_rx_delta"), b.get("net_tx_delta"),
                            b.get("samples", 0),
                        ),
                    )
                    if cur.rowcount:
                        buckets_merged += 1
                elif "date" in b:
                    # Daily-summary-style entry
                    cur = self._conn.execute(
                        """INSERT OR REPLACE INTO daily_summaries
                        (peer_node_id, date, uptime_pct, avg_cpu, avg_mem, incidents)
                        VALUES (?,?,?,?,?,?)""",
                        (
                            peer_node_id,
                            str(b.get("date", "")),
                            b.get("uptime_pct"),
                            b.get("avg_cpu"),
                            b.get("avg_mem"),
                            b.get("incidents", 0),
                        ),
                    )
                    if cur.rowcount:
                        buckets_merged += 1
        return {
            "snapshots_merged": snapshots_merged,
            "events_merged": events_merged,
            "buckets_merged": buckets_merged,
            "strategy": strategy,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _aggregate_snaps(
        self,
        snaps: list[dict],
        window_start: int,
        window_end: int,
        bucket_size: int,
    ) -> StatBucket:
        cpu_vals = [s["cpu_percent"] for s in snaps if "cpu_percent" in s]
        mem_vals = [s["mem_percent"] for s in snaps if "mem_percent" in s]
        disk_vals = [s["disk_percent"] for s in snaps if "disk_percent" in s]
        net_rx_vals = [s["net_recv_bytes"] for s in snaps if "net_recv_bytes" in s]
        net_tx_vals = [s["net_sent_bytes"] for s in snaps if "net_sent_bytes" in s]

        def avg(lst: list) -> Optional[float]:
            return round(sum(lst) / len(lst), 2) if lst else None

        net_rx_delta = (net_rx_vals[-1] - net_rx_vals[0]) if len(net_rx_vals) >= 2 else None
        net_tx_delta = (net_tx_vals[-1] - net_tx_vals[0]) if len(net_tx_vals) >= 2 else None

        return StatBucket(
            window_start=_from_epoch(window_start),
            window_end=_from_epoch(window_end),
            bucket_size=bucket_size,
            cpu_avg=avg(cpu_vals),
            cpu_max=max(cpu_vals) if cpu_vals else None,
            mem_avg=avg(mem_vals),
            mem_max=max(mem_vals) if mem_vals else None,
            disk_pct_avg=avg(disk_vals),
            net_rx_delta=net_rx_delta,
            net_tx_delta=net_tx_delta,
            samples=len(snaps),
        )
