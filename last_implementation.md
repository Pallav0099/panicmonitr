# panic-monitr v2 — Implementation Plan

## Current State Summary

| Module | Lines | Role |
|---|---|---|
| [engine.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/engine.py) | 964 | Iroh node, heartbeat probes, push protocol, threshold state machine |
| [schema.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/schema.py) | 65 | PeerEntry, PeerState, LatencyRecord, PeerStatus |
| [history.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/history.py) | 318 | SQLite probe store (raw rows), uptime %, hourly buckets |
| [tui.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/tui.py) | 942 | Textual TUI — table + detail pane + events feed |
| [statuspage.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/statuspage.py) | 968 | stdlib HTTP server, inline HTML dashboard, `/status.json` API |
| [log.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/log.py) | 312 | Signed append-only trust log (JSONL) |
| [trust.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/trust.py) | 357 | Peer trust manager projected from log |
| [main.py](file:///home/pallav/Projects/summer-break/panic-monitr/main.py) | 685 | CLI arg parser + entry point |

**What works today:** P2P alive/dead heartbeat monitoring across NAT via iroh. Probes, thresholds, webhooks, maintenance windows, tags, uptime history, TUI, HTML dashboard.

**What's missing:** System stats, role clarity, offline gap handling, gossip-based stat streaming, server-side log store.

---

## Architecture: The Two Roles

```
┌─────────────────────────────┐       ┌─────────────────────────────┐
│   MONITORED NODE (agent)    │       │   MONITORING NODE (dash)    │
│                             │       │                             │
│  • Collects own stats       │ gossip│  • Receives gossip packets  │
│    (psutil + docker-py)     │──────▶│  • Renders TUI / Web UI     │
│  • Writes local log store   │       │  • Does NOT own truth       │
│  • Broadcasts snapshots     │       │  • Shows "dashboard gap"    │
│  • Serves sync responses    │◀──────│    when itself was offline   │
│  • Source of truth          │ sync  │  • Requests sync on reconn  │
│  • Runs 24/7 regardless     │request│                             │
└─────────────────────────────┘       └─────────────────────────────┘
```

> [!IMPORTANT]
> A single node can be **both** monitored and monitoring. The `--role` flag defaults to `both`. The key insight: truth lives on the monitored side.

---

## Phase 0 — Role Flags & Config

**Files:** [main.py](file:///home/pallav/Projects/summer-break/panic-monitr/main.py), [engine.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/engine.py)

- Add `--role` arg: `monitored | monitoring | both` (default: `both`)
- Add `--dashboard-port` arg (default: `42069`) for the Flask web UI
- Add `--stats-interval` arg (default: `10` seconds) for stat collection cadence
- Engine stores `self._role` and conditionally starts subsystems

---

## Phase 1 — System Stats Collector (`src/stats.py`)

**New file.** Depends on: `psutil`, `docker` (docker-py)

### 1a. `SystemSnapshot` dataclass

```python
@dataclass
class SystemSnapshot:
    timestamp: str          # ISO 8601
    hostname: str
    # CPU
    cpu_percent: float      # 0-100, all cores
    cpu_count: int
    load_avg: tuple[float, float, float]  # 1m, 5m, 15m
    # Memory
    mem_total_bytes: int
    mem_used_bytes: int
    mem_percent: float
    # Swap
    swap_total_bytes: int
    swap_used_bytes: int
    # Disk
    disk_total_bytes: int
    disk_used_bytes: int
    disk_percent: float
    disk_io_read_bytes: int
    disk_io_write_bytes: int
    # Network
    net_sent_bytes: int
    net_recv_bytes: int
    # Temps (optional)
    cpu_temp: float | None
    # Process count
    process_count: int
```

### 1b. `ContainerSnapshot` dataclass

```python
@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    status: str        # running, exited, paused, etc.
    health: str | None # healthy, unhealthy, starting, none
    cpu_percent: float
    mem_usage_bytes: int
    mem_limit_bytes: int
    net_rx_bytes: int
    net_tx_bytes: int
    uptime_seconds: int
    restart_count: int
```

### 1c. `StatsCollector` class

- `collect_system() -> SystemSnapshot` — one psutil call
- `collect_containers() -> list[ContainerInfo]` — docker socket, graceful fallback if docker not available
- `collect_all() -> dict` — combined payload, serializable
- Runs in `asyncio.to_thread()` to avoid blocking the event loop
- Catches all exceptions per-subsystem (no crash if docker is absent)

### New dependencies

```
psutil>=5.9
docker>=7.0
flask>=3.0
plotly>=5.18
```

---

## Phase 2 — Server-Side Log Store (`src/logstore.py`)

**New file.** SQLite-backed, runs on the **monitored** node.

### Schema

```sql
-- Event log: state transitions only
CREATE TABLE events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,  -- unix epoch
    event     TEXT NOT NULL,      -- agent_started, container_exited, etc.
    detail    TEXT               -- JSON blob
);
CREATE INDEX idx_events_ts ON events(ts);

-- Rolled-up stat buckets (5-min default)
CREATE TABLE stat_buckets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    bucket_size  INTEGER NOT NULL,  -- seconds (300 = 5min, 3600 = 1hr)
    cpu_avg    REAL,
    cpu_max    REAL,
    mem_avg    REAL,
    mem_max    REAL,
    disk_avg   REAL,
    net_rx     INTEGER,
    net_tx     INTEGER,
    samples    INTEGER
);
CREATE INDEX idx_buckets_window ON stat_buckets(window_start);

-- Daily summaries
CREATE TABLE daily_summaries (
    date       TEXT PRIMARY KEY,  -- YYYY-MM-DD
    uptime_pct REAL,
    avg_cpu    REAL,
    avg_mem    REAL,
    incidents  INTEGER
);

-- Raw snapshot ring buffer (last 2 hours)
CREATE TABLE raw_snapshots (
    ts      INTEGER PRIMARY KEY,
    payload TEXT NOT NULL  -- JSON
);
```

### `LogStore` class

| Method | Description |
|---|---|
| `record_snapshot(snap)` | Insert into `raw_snapshots`, prune >2hr old |
| `record_event(event, detail)` | Insert into `events` |
| `roll_up()` | Aggregate raw → 5-min buckets, called every 5 min |
| `roll_hourly()` | Aggregate 5-min → 1-hour buckets for data >24h old |
| `roll_daily()` | Aggregate hourly → daily summaries for data >7d old |
| `get_sync_payload(last_seen_ts)` | Compute gap, return appropriate data per decision tree |
| `prune()` | Enforce retention: 2hr raw, 30d 5-min, hourly forever |

### Retention & rollup schedule (APScheduler jobs)

| Interval | Job |
|---|---|
| 5 min | `roll_up()` — raw snapshots → 5-min buckets |
| 1 hour | `roll_hourly()` — 5-min buckets >24h → hourly |
| 24 hours | `roll_daily()` — hourly >7d → daily summaries |
| 1 hour | `prune()` — enforce retention limits |

---

## Phase 3 — Gossip Stat Streaming

**Files:** [engine.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/engine.py)

### 3a. New ALPN protocols

```python
STATS_GOSSIP_ALPN = b"panic-monitor/stats-gossip/0"
SYNC_ALPN         = b"panic-monitor/sync/0"
```

### 3b. Gossip broadcast (monitored node)

Every `--stats-interval` seconds:
1. `StatsCollector.collect_all()` in thread pool
2. `LogStore.record_snapshot()` — persist locally
3. Broadcast via iroh-gossip to topic (derived from node_id)
4. Message format: `{"type": "stats_snapshot", "node_id": "...", "data": {...}}`

### 3c. Gossip subscription (monitoring node)

- Subscribe to each monitored peer's topic on startup
- On receive: update in-memory `PeerState` with latest stats
- Feed into TUI/web dashboard render loop

### 3d. Sync protocol (on reconnect)

**Sync handshake via gossip → bulk transfer via direct ALPN stream:**

```
Monitor comes online
    │
    ├─ broadcasts: SyncRequest { node_id, last_seen_ts }
    │
    ▼
Monitored node receives, computes gap
    │
    ├─ opens direct ALPN stream (SYNC_ALPN)
    │
    ▼
Sends: SyncResponse {
    gap_start, gap_end,
    events: [Event],
    buckets: [StatBucket],
    sync_strategy: "raw" | "5min" | "1hour" | "daily"
}
```

**Gap decision tree:**

| Gap Duration | Sync Strategy | Data Sent |
|---|---|---|
| < 1 hour | `raw` | Raw snapshots for gap period |
| 1h – 24h | `5min` | 5-min buckets + all events + last 30min raw |
| 24h – 7d | `1hour` | 1-hour buckets + all events |
| > 7d | `daily` | Daily summaries + events only |

---

## Phase 4 — Schema Upgrades (`src/schema.py`)

Extend `PeerState` to hold system stats:

```python
class PeerState:
    __slots__ = (
        # ... existing slots ...
        "last_stats",           # SystemSnapshot | None
        "containers",           # list[ContainerInfo]
        "stats_history",        # deque[SystemSnapshot] (maxlen=360 = 1hr @ 10s)
        "sync_status",          # "live" | "syncing" | "gap" | "synced"
        "last_sync_ts",         # datetime | None
        "role",                 # "monitored" | "monitoring" | "both"
    )
```

Extend `PeerStatus`:

```python
class PeerStatus(str, enum.Enum):
    ALIVE = "ALIVE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"
    UNREACHABLE = "UNREACHABLE"  # new: network issue, not confirmed down
```

---

## Phase 5 — The Ambiguity Fix

### Three cases, three visual states

| Case | What happened | Display | Color |
|---|---|---|---|
| A | Monitored node actually went down | `● DEAD` | Red |
| B | Monitoring node was offline | `◌ DASHBOARD GAP` | Grey, hatched |
| C | Network broke, both sides up | `◎ UNREACHABLE` | Amber |

### Implementation

1. **Monitored node** writes `agent_started` / `agent_shutdown` events to its local log store
2. **On sync**, monitoring node receives the event log → can distinguish:
   - Events exist during gap → **Case B** (dash was offline, server was fine)
   - No events + no heartbeats → **Case A** (real outage)
   - Heartbeat fails but sync shows server was up → **Case C** (network issue)
3. `PeerState.sync_status` tracks whether gap data has been backfilled
4. Dashboard renders synced-history with **different visual density** (▓ vs █)

---

## Phase 6 — TUI Overhaul (btop/htop inspired)

**File:** [tui.py](file:///home/pallav/Projects/summer-break/panic-monitr/src/tui.py)

### Layout (btop-inspired)

```
┌─ PANIC MONITR ──────────────────────────────────────────────────────┐
│ [q] quit  [r] refresh  [p] add peer  [1-4] sort  [tab] focus       │
├─────────────────────────────────────────────────────────────────────┤
│ MONITOR 3  ALIVE 2  DEAD 1  AVG CPU 14%  AVG MEM 4.1GB  PROBES 2.8k│
├──────────────────────────────┬──────────────────────────────────────┤
│  # ALIAS    STATUS  CPU  MEM│  homeserver ● ALIVE                  │
│  01 homesrv ● ALIVE 12% 4.1G│  ┌─ CPU ─────────────────────────┐  │
│  02 vpn-eu  ● ALIVE  3% 0.8G│  │ ▁▂▃▄▅▆▅▄▃▂▁▂▃▅▆▇▆▅▄▃▂▁▂▃▄▅ │  │
│  03 backup  ● DEAD   — — — │  │ 12% avg  34% max  4 cores     │  │
│                              │  ├─ MEM ─────────────────────────┤  │
│                              │  │ ████████████░░░░░░░░ 4.1/8 GB │  │
│                              │  ├─ DISK ────────────────────────┤  │
│                              │  │ ██████████████░░░░░░ 120/240G │  │
│                              │  ├─ NET ─────────────────────────┤  │
│                              │  │ ↑ 1.2 MB/s  ↓ 340 KB/s       │  │
│                              │  ├─ CONTAINERS ──────────────────┤  │
│                              │  │ nginx     ● running  0.3% 120M│  │
│                              │  │ postgres  ● running  2.1% 1.2G│  │
│                              │  │ redis     ● running  0.1%  64M│  │
├──────────────────────────────┴──────────────────────────────────────┤
│ ── TIMELINE ── 24h ─────────────────────────────────────────────────│
│ ████████████░░░░░░░░░░▓▓▓▓▓▓▓▓████████████████████                  │
│             gap(you)  synced   live                                  │
├─────────────────────────────────────────────────────────────────────┤
│ EVENTS  #42 2m ago monitor_up homesrv count=1                       │
│         #41 1h ago monitor_down homesrv count=3 TimeoutError        │
└─────────────────────────────────────────────────────────────────────┘
```

### Key changes to TUI

- Left pane: peer table gains `CPU`, `MEM` columns from live gossip stats
- Right pane: selected peer detail with CPU/MEM/DISK/NET sparkline bars
- Container list in detail pane
- Timeline bar with three visual states (live █, synced ▓, gap ░)
- New keybinds: `[1-4]` sort by status/cpu/mem/name, `[tab]` focus switch

---

## Phase 7 — Flask + Plotly Web Dashboard (`src/webapp.py`)

**New file.** Port `42069`.

### Architecture

```
Flask app (threaded, daemon thread — same as current stdlib HTTP)
  ├─ GET /                → Jinja2 rendered HTML (Plotly.js inline)
  ├─ GET /api/status      → full dashboard JSON (replaces /status.json)
  ├─ GET /api/history     → per-peer time series
  ├─ GET /api/containers  → container list for a peer
  └─ GET /api/timeline    → gap/synced/live timeline data
```

### Dashboard page layout (Plotly)

| Section | Plotly chart type |
|---|---|
| Header stats strip | Static cards (same design as current HTML) |
| Per-peer CPU timeline | `Scatter` with fill, 24h rolling |
| Per-peer Memory gauge | `Indicator` gauge |
| Disk usage | `Bar` chart |
| Network throughput | Dual-axis `Scatter` (rx/tx) |
| Container grid | HTML table with status badges |
| Uptime timeline | Custom `Heatmap` (█ ▓ ░ states) |
| Events feed | HTML table, same as current |
| RTT chart | Keep existing SVG approach (it's already good) |

### Design notes

- Same SkyTunnel ember palette (dark bg, amber accents, teal for healthy)
- JetBrains Mono font
- Auto-refresh every 2s via JS `setInterval` + `Plotly.react()` (no full re-render)
- Responsive grid, same quality as current HTML dashboard
- The current stdlib `StatusPageServer` stays for backward compat on `--status-bind`
- Flask serves on `--dashboard-port` (42069)

---

## Phase 8 — Migration Path

### Backward compatibility

- Existing `--daemon` / `--tui` modes work unchanged if `--role` is not specified
- Default role `both` means: collect own stats + monitor peers (current behavior + stats)
- `--status-bind` (old HTTP dashboard) remains functional
- New `--dashboard-port` is additive

### New CLI flags summary

```
--role {monitored,monitoring,both}   Default: both
--dashboard-port PORT                Default: 42069 (Flask+Plotly)
--stats-interval SECS                Default: 10
--logstore-db PATH                   Default: ./logstore.db
--no-docker                          Skip docker stats collection
```

---

## Execution Order

| Phase | Effort | Dependencies |
|---|---|---|
| **P0** Role flags | Small | None |
| **P1** Stats collector | Medium | psutil, docker-py |
| **P2** Log store | Medium | P1 |
| **P3** Gossip streaming + sync | Large | P1, P2 |
| **P4** Schema upgrades | Small | P1 |
| **P5** Ambiguity fix | Medium | P2, P3 |
| **P6** TUI overhaul | Large | P1, P4 |
| **P7** Flask + Plotly dashboard | Large | P1, P4 |
| **P8** Migration / integration | Small | All |

> [!TIP]
> **Recommended build order:** P0 → P1 → P4 → P2 → P6 (TUI, immediate visual payoff) → P7 (web) → P3 (gossip, hardest) → P5 (ambiguity, needs P3) → P8

---

## Storage Budget

| Data type | Size | Retention |
|---|---|---|
| Raw snapshots (10s) | ~500 B each → 3 MB/hr | 2 hours rolling |
| 5-min buckets | ~100 B each → 29 KB/day | 30 days |
| 1-hour buckets | ~100 B each → 2.4 KB/day | Forever |
| Daily summaries | ~80 B each | Forever |
| Events | ~50 B each | Forever |
| **Total per node per year** | **< 5 MB** | — |
