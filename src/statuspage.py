from __future__ import annotations

import http.server
import json
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional
from urllib.parse import parse_qs, urlparse

from loguru import logger

from src import IST
from src.schema import PeerStatus

if TYPE_CHECKING:
    from src.engine import MonitorEngine

# ---------------------------------------------------------------------------
# Shared ASCII banner — sourced byte-for-byte from the user's master copy
# at ~/panicmonitr.txt. Trimmed of trailing whitespace and a final newline so
# the HTML `<pre>` and the TUI rich-markup wrapper both render identically.
# ---------------------------------------------------------------------------
ASCII_BANNER = (
    "\u2588\u2580\u2588 \u2588\u2580\u2588 \u2588\u2584 \u2588 \u2588 "
    "\u2588\u2580\u2580 \u2588\u2580\u2584\u2580\u2588 \u2588\u2580\u2588 "
    "\u2588\u2584 \u2588 \u2588 \u2580\u2588\u2580 \u2588\u2580\u2588\n"
    "\u2588\u2580\u2580 \u2588\u2580\u2588 \u2588 \u2580\u2588 \u2588 "
    "\u2588\u2584\u2584 \u2588 \u2580 \u2588 \u2588\u2584\u2588 "
    "\u2588 \u2580\u2588 \u2588  \u2588  \u2588\u2580\u2584\n"
    "\u2580   \u2580 \u2580 \u2580  \u2580 \u2580 \u2580\u2580\u2580 "
    "\u2580   \u2580 \u2580\u2580\u2580 \u2580  \u2580 \u2580  \u2580  \u2580 \u2580"
)


# ---------------------------------------------------------------------------
# Dashboard snapshot (reused by HTTP handler and P2P status ALPN)
# ---------------------------------------------------------------------------

_SPARKLINE_POINTS = 120
_EVENTS_FEED_LIMIT = 30


def build_dashboard_snapshot(engine: "MonitorEngine") -> dict:
    """One JSON-ready view of this device's monitoring state.

    Local-only: every field is derived from in-process state, history.db,
    or peers.json — nothing hits the network.
    """
    trust = engine.trust
    history = engine.history
    devices = engine.get_device_states()
    now = datetime.now(IST)

    peers_out: list[dict] = []
    avg_uptime_sum = 0.0
    avg_uptime_count = 0
    maint_count = 0
    for state in devices:
        trusted = trust.get_peer(state.entry.node_id)
        node_id = state.entry.node_id
        in_maint = trusted.in_maintenance(now) if trusted is not None else False
        if in_maint:
            maint_count += 1

        uptime = {
            "1h":  history.uptime_percent(node_id, timedelta(hours=1)),
            "24h": history.uptime_percent(node_id, timedelta(hours=24)),
            "7d":  history.uptime_percent(node_id, timedelta(days=7)),
            "30d": history.uptime_percent(node_id, timedelta(days=30)),
        }
        if uptime["24h"] is not None:
            avg_uptime_sum += uptime["24h"]
            avg_uptime_count += 1

        hourly = history.hourly_uptime_buckets(node_id, hours=24)
        rtt = history.rtt_stats(node_id, hours=24)

        # Prefer the in-memory deque for the live sparkline (newest data);
        # fall back to disk if the peer was just added.
        spark_src = list(state.latency_history)[-_SPARKLINE_POINTS:]
        if not spark_src:
            spark_src = [
                # conform to the same shape as LatencyRecord in deque
                type("R", (), {
                    "timestamp": r.ts, "rtt_ms": r.rtt_ms, "status": r.status,
                })()
                for r in history.recent_rows(node_id, hours=6)[-_SPARKLINE_POINTS:]
            ]
        sparkline = [
            {
                "ts": r.timestamp.isoformat(),
                "rtt_ms": r.rtt_ms,
                "status": r.status.value,
            }
            for r in spark_src
        ]

        current_rtt = (
            state.latency_history[-1].rtt_ms if state.latency_history else None
        )

        peers_out.append(
            {
                "node_id": node_id,
                "alias": state.entry.alias,
                "tags": list(trusted.tags) if trusted else [],
                "status": state.current_status.value,
                "in_maintenance": in_maint,
                "maintenance_start": (
                    trusted.maintenance_start.isoformat()
                    if trusted and trusted.maintenance_start else None
                ),
                "maintenance_end": (
                    trusted.maintenance_end.isoformat()
                    if trusted and trusted.maintenance_end else None
                ),
                "last_seen": state.last_seen.isoformat() if state.last_seen else None,
                "consecutive_failures": state.consecutive_failures,
                "consecutive_successes": state.consecutive_successes,
                "uptime": uptime,
                "hourly_24h": hourly,
                "rtt": {
                    "current": current_rtt,
                    "min_24h": rtt["rtt_min"],
                    "max_24h": rtt["rtt_max"],
                    "avg_24h": rtt["rtt_avg"],
                    "probes_24h": rtt["probes"],
                    "alive_24h": rtt["alive"],
                    "dead_24h": rtt["dead"],
                },
                "sparkline": sparkline,
            }
        )

    all_peers = trust.list_peers()
    recent_events = log.monitor_events() if (log := engine.log) else []
    recent_events = recent_events[-_EVENTS_FEED_LIMIT:][::-1]  # newest first
    events_out = [
        {
            "seq": e.seq,
            "type": e.type,
            "timestamp": e.timestamp,
            "data": e.data,
        }
        for e in recent_events
    ]

    alias_by_nid = {p.node_id: p.alias for p in all_peers}
    for ev in events_out:
        nid = ev["data"].get("node_id") if isinstance(ev.get("data"), dict) else None
        ev["peer_alias"] = alias_by_nid.get(nid) if nid else None
        ev["peer_node_id"] = nid

    probes_24h = engine.history.count_in_window(24)
    avg_uptime_24h = (
        avg_uptime_sum / avg_uptime_count if avg_uptime_count else None
    )

    return {
        "source": {
            "node_id": engine.node_id,
            "interval_seconds": engine._interval,
            "down_after": engine._down_after,
            "up_after": engine._up_after,
            "flap_min_dwell_seconds": engine._flap_dwell_seconds,
        },
        "now": now.isoformat(),
        "counts": {
            "monitor_targets": len(devices),
            "alive": sum(
                1 for d in devices if d.current_status == PeerStatus.ALIVE
            ),
            "dead": sum(
                1 for d in devices if d.current_status == PeerStatus.DEAD
            ),
            "unknown": sum(
                1 for d in devices if d.current_status == PeerStatus.UNKNOWN
            ),
            "maintenance": maint_count,
            "peers_total": sum(1 for p in all_peers if p.revoked_at is None),
            "peers_revoked": sum(
                1 for p in all_peers if p.revoked_at is not None
            ),
        },
        "totals": {
            "probes_24h": probes_24h,
            "avg_uptime_24h": avg_uptime_24h,
            "events_last_24h": sum(
                1 for e in events_out
                if (now - datetime.fromisoformat(e["timestamp"])).total_seconds() <= 86400
            ),
        },
        "peers": peers_out,
        "events": events_out,
    }


# ---------------------------------------------------------------------------
# HTML page — single file, no build step, Google Fonts only external dep.
# Theme: skytunnel-website palette (confirmed to match the TUI constants).
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>panic-monitor · dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0c0b0f;
  --panel: #16141c;
  --panel-strong: #1e1b26;
  --border: #2a2520;
  --border-soft: rgba(255,240,210,0.06);
  --border-strong: rgba(220,130,40,0.42);
  --accent: #dc8228;
  --accent2: #f8a83e;
  --accent-title: #ee9434;
  --teal: #2ac0a8;
  --teal-dim: #1e7f71;
  --red: #d24141;
  --red-dim: #802a2a;
  --text-bright: #f2ecde;
  --text-primary: #cdc3b2;
  --text-muted: #948873;
  --text-dim: #605646;
  --text-faint: #3c352a;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  background-color: var(--bg);
  background-image:
    linear-gradient(rgba(255,240,200,0.038) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,240,200,0.038) 1px, transparent 1px);
  background-size: 32px 32px;
  color: var(--text-primary);
  font-family: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
  font-size: 13px; line-height: 1.5;
  font-weight: 500;   /* everything is a touch heavier; values lift further */
  min-height: 100vh;
}
.container { max-width: 1400px; margin: 0 auto; padding: 1.75rem 1.5rem 3rem; }

/* ---- header ------------------------------------------------------------- */
header { margin-bottom: 1.5rem; }
pre.banner {
  color: var(--accent); font-weight: 700; font-size: 12px;
  line-height: 1.05; white-space: pre;
  filter: drop-shadow(0 0 8px rgba(220,130,40,0.45));
  margin-bottom: 0.9rem; overflow-x: auto;
}
.tagline {
  color: var(--text-muted); text-transform: uppercase;
  letter-spacing: 0.22em; font-size: 10px; font-weight: 600;
}
.tagline .sep { color: var(--text-faint); }
.tagline .pill {
  display: inline-block; padding: 2px 8px;
  border: 1px solid var(--border-strong); background: var(--panel);
  color: var(--accent); letter-spacing: 0.12em; font-weight: 700;
  margin-right: 0.4rem;
}

/* ---- hero stats strip --------------------------------------------------- */
.stats-strip {
  display: grid;
  grid-template-columns: repeat(8, 1fr);
  gap: 0;
  border: 1px solid var(--border);
  background: var(--panel);
  margin-bottom: 1.5rem;
}
.tile {
  padding: 0.9rem 1rem;
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; justify-content: center;
  min-height: 74px;
}
.tile:last-child { border-right: 0; }
.tile .label {
  color: var(--text-muted); text-transform: uppercase;
  letter-spacing: 0.2em; font-size: 9.5px; font-weight: 700;
  margin-bottom: 0.25rem;
}
.tile .value { font-size: 22px; font-weight: 700; color: var(--text-bright); font-variant-numeric: tabular-nums; letter-spacing: 0.01em; }
.tile .sub { color: var(--text-muted); font-size: 10px; margin-top: 0.2rem; text-transform: uppercase; letter-spacing: 0.14em; font-weight: 600; }
.tile .value.teal  { color: var(--teal); }
.tile .value.red   { color: var(--red); }
.tile .value.accent{ color: var(--accent); }
.tile .value.dim   { color: var(--text-dim); font-size: 14px; }
@media (max-width: 1100px) {
  .stats-strip { grid-template-columns: repeat(4, 1fr); }
  .tile { border-bottom: 1px solid var(--border); }
  .tile:nth-last-child(-n+4) { border-bottom: 0; }
}

/* ---- section headers ---------------------------------------------------- */
.section-head {
  color: var(--text-muted); text-transform: uppercase;
  letter-spacing: 0.2em; font-size: 10.5px; font-weight: 700;
  margin: 2rem 0 0.6rem; display: flex; align-items: center; gap: 0.75rem;
}
.section-head .tick { color: var(--text-faint); flex: 1; border-bottom: 1px dashed var(--border); min-width: 20px; height: 1px; }
.section-head .cmd { color: var(--accent); }

/* ---- peer grid ---------------------------------------------------------- */
.peers-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(560px, 1fr));
  gap: 1rem;
}
.peer-card {
  border: 1px solid var(--border);
  background: var(--panel);
  padding: 0.9rem 1.1rem 0.95rem;
  display: flex; flex-direction: column; gap: 0.65rem;
  transition: border-color 150ms ease;
}
.peer-card:hover { border-color: var(--border-strong); }
.peer-card.maint { border-left: 3px solid var(--accent); }
.peer-card.dead  { border-left: 3px solid var(--red); }
.peer-card.alive { border-left: 3px solid var(--teal); }
.peer-card.unknown { border-left: 3px solid var(--text-dim); }

.peer-head {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.6rem 1rem;
}
.peer-head .alias {
  color: var(--text-bright); font-weight: 700;
  font-size: 16px; letter-spacing: 0.01em;
}
.peer-head .tags { color: var(--accent2); font-size: 11px; font-weight: 700; letter-spacing: 0.07em; }
.peer-head .status {
  margin-left: auto; font-weight: 700; letter-spacing: 0.16em; font-size: 11px;
  padding: 3px 10px; border: 1px solid var(--border); text-transform: uppercase;
}
.status.alive   { color: var(--teal); border-color: var(--teal-dim); }
.status.dead    { color: var(--red);  border-color: var(--red-dim); }
.status.maint   { color: var(--accent); border-color: var(--border-strong); }
.status.unknown { color: var(--text-dim); }

.peer-nid {
  font-size: 10.5px; color: var(--text-muted); font-weight: 500;
  letter-spacing: 0.04em; font-family: 'JetBrains Mono', monospace;
  word-break: break-all;
}

/* uptime row */
.uptime-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0.25rem;
  background: var(--bg);
  border: 1px solid var(--border);
}
.uptime-row .cell {
  padding: 0.55rem 0.7rem; text-align: center;
  border-right: 1px solid var(--border);
}
.uptime-row .cell:last-child { border-right: 0; }
.uptime-row .cell .n {
  font-size: 18px; font-weight: 700; color: var(--text-bright);
  font-variant-numeric: tabular-nums; letter-spacing: 0.02em;
}
.uptime-row .cell .l {
  font-size: 9px; color: var(--text-muted); font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.22em; margin-top: 3px;
}
.n.good { color: var(--teal); }
.n.warn { color: var(--accent2); }
.n.bad  { color: var(--red); }
.n.muted{ color: var(--text-dim); }

/* 24h hourly bar */
.hourly {
  display: grid;
  grid-template-columns: repeat(24, 1fr);
  gap: 1px;
  height: 18px;
}
.hourly .bar {
  background: var(--text-faint);
  cursor: default;
}
.hourly .bar.g  { background: var(--teal); }
.hourly .bar.w  { background: var(--accent2); }
.hourly .bar.b  { background: var(--red); }
.hourly .bar.n  { background: var(--panel-strong); }
.hourly-legend {
  display: flex; justify-content: space-between;
  color: var(--text-dim); font-size: 9px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.18em;
  margin-top: 3px;
}

/* response-time chart + rtt stats */
.rtt-chart-wrap {
  background: var(--bg);
  border: 1px solid var(--border);
  padding: 4px 4px 0;
  position: relative;
}
svg.rtt-chart {
  width: 100%; height: 96px; display: block;
}
svg.rtt-chart .axis { stroke: var(--border); stroke-width: 1; }
svg.rtt-chart .grid { stroke: var(--border-soft); stroke-width: 1; stroke-dasharray: 2 3; }
svg.rtt-chart .tick {
  fill: var(--text-muted); font-size: 9px;
  font-family: 'JetBrains Mono', monospace; font-weight: 700;
  letter-spacing: 0.08em; text-transform: uppercase;
}
svg.rtt-chart .area { fill: var(--accent); fill-opacity: 0.15; }
svg.rtt-chart .line { fill: none; stroke: var(--accent2); stroke-width: 1.6; stroke-linejoin: round; stroke-linecap: round; }
svg.rtt-chart .dead-dot { fill: var(--red); }
.chart-empty {
  color: var(--text-faint); font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.22em;
  text-align: center; padding: 2rem 0;
}

.peer-metrics {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0.55rem;
}
.rtt-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0.3rem;
  background: var(--bg);
  border: 1px solid var(--border);
}
.rtt-grid .cell {
  padding: 0.45rem 0.5rem;
  border-right: 1px solid var(--border);
  text-align: center;
}
.rtt-grid .cell:last-child { border-right: 0; }
.rtt-grid .k {
  color: var(--text-muted); text-transform: uppercase;
  letter-spacing: 0.22em; font-size: 8.5px; font-weight: 700;
  display: block; margin-bottom: 2px;
}
.rtt-grid .v {
  color: var(--text-bright); font-variant-numeric: tabular-nums;
  font-weight: 700; font-size: 13px; letter-spacing: 0.01em;
}

/* peer footer */
.peer-foot {
  display: flex; flex-wrap: wrap; gap: 0.55rem 1.4rem;
  border-top: 1px dashed var(--border);
  padding-top: 0.6rem;
  font-size: 10.5px; color: var(--text-primary);
  letter-spacing: 0.04em;
}
.peer-foot .k { color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.18em; margin-right: 0.4rem; font-size: 9px; font-weight: 700; }
.peer-foot .v { color: var(--text-bright); font-variant-numeric: tabular-nums; font-weight: 700; }
.peer-foot .v.warn { color: var(--red); }

.maint-badge {
  color: var(--accent); font-size: 10.5px; font-weight: 700;
  letter-spacing: 0.2em; text-transform: uppercase;
}

/* ---- events feed -------------------------------------------------------- */
.events-panel {
  border: 1px solid var(--border);
  background: var(--panel);
  overflow-x: auto;
}
.events-panel table { width: 100%; border-collapse: collapse; font-size: 12px; }
.events-panel thead tr { background: var(--panel-strong); }
.events-panel th {
  color: var(--accent); text-align: left;
  text-transform: uppercase; letter-spacing: 0.2em;
  font-weight: 700; font-size: 10px;
  padding: 0.6rem 0.95rem;
  border-bottom: 1px solid var(--border);
}
.events-panel td {
  padding: 0.55rem 0.95rem;
  color: var(--text-primary); font-weight: 600;
  border-bottom: 1px solid var(--border-soft);
  vertical-align: middle;
  font-variant-numeric: tabular-nums;
}
.events-panel tbody tr:last-child td { border-bottom: 0; }
.events-panel .evtype.up   { color: var(--teal);  font-weight: 700; }
.events-panel .evtype.down { color: var(--red);   font-weight: 700; }
.events-panel .seq { color: var(--text-faint); }
.events-panel .empty-row td { text-align: center; color: var(--text-faint); padding: 1.75rem; text-transform: uppercase; letter-spacing: 0.2em; font-size: 10.5px; }

/* ---- footer ------------------------------------------------------------- */
.footer {
  color: var(--text-faint); font-size: 10px;
  text-align: center; margin-top: 2rem;
  text-transform: uppercase; letter-spacing: 0.22em;
}
.footer code { background: var(--panel); padding: 1px 5px; border: 1px solid var(--border); color: var(--text-dim); }

.empty-peers {
  border: 1px dashed var(--border);
  padding: 3rem 1.5rem;
  color: var(--text-faint);
  text-align: center;
  text-transform: uppercase; letter-spacing: 0.22em; font-size: 11px;
}
</style>
</head>
<body>
<div class="container">
<header>
  <pre class="banner">__BANNER__</pre>
  <div class="tagline">
    <span class="pill">p2p heartbeat</span>
    <span>local state only</span>
    <span class="sep">·</span>
    <span>no central server</span>
    <span class="sep">·</span>
    <span id="clock">\u2014</span>
  </div>
</header>

<section id="stats-strip" class="stats-strip"></section>

<div class="section-head">
  <span class="tick"></span>
  <span class="cmd">$ ./peers</span>
  <span class="tick"></span>
</div>
<section id="peers-grid" class="peers-grid"></section>

<div class="section-head">
  <span class="tick"></span>
  <span class="cmd">$ tail -f log.jsonl | jq 'select(.type | test("monitor"))'</span>
  <span class="tick"></span>
</div>
<section class="events-panel">
  <table>
    <thead><tr>
      <th>seq</th><th>when</th><th>event</th><th>peer</th><th>count</th><th>reason</th>
    </tr></thead>
    <tbody id="events-rows"></tbody>
  </table>
</section>

<div class="footer">panic-monitor <span class="sep">·</span> auto-refresh 2s <span class="sep">·</span> reads <code>history.db</code> <code>log.jsonl</code> <code>peers.json</code></div>
</div>

<script>
const SPARK = '\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588';
const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

function fmtTime(iso) {
  if (!iso) return 'never';
  const d = new Date(iso); if (isNaN(d)) return '—';
  return d.toLocaleTimeString([], {hour12: false});
}
function fmtRel(iso) {
  if (!iso) return 'never';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60)    return Math.floor(s) + 's ago';
  if (s < 3600)  return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}
function fmtMs(v) {
  if (v == null) return '\u2014';
  return v.toFixed(v < 10 ? 2 : 1) + 'ms';
}
function fmtPct(v) {
  if (v == null) return '\u2014';
  return v.toFixed(v === 100 ? 0 : 2) + '%';
}
function pctClass(v) {
  if (v == null) return 'muted';
  if (v >= 99.9) return 'good';
  if (v >= 95)   return 'warn';
  return 'bad';
}
function bucketClass(v) {
  if (v == null)   return 'n';
  if (v >= 99.9)   return 'g';
  if (v >= 95)     return 'w';
  return 'b';
}
function sparkline(rows) {
  /* legacy block sparkline, kept for reference in case we want a compact inline view */
  if (!rows || !rows.length) return '';
  const rtts = rows.filter(r => r.status === 'ALIVE' && r.rtt_ms != null).map(r => r.rtt_ms);
  if (!rtts.length) return rows.map(()=>'\u00b7').join('');
  const lo = Math.min(...rtts), hi = Math.max(...rtts), span = (hi - lo) || 1;
  return rows.map(r => {
    if (r.status !== 'ALIVE' || r.rtt_ms == null) return '\u00b7';
    const n = (r.rtt_ms - lo) / span;
    return SPARK[Math.max(0, Math.min(SPARK.length-1, Math.floor(n*(SPARK.length-1))))];
  }).join('');
}

function rttChart(rows) {
  /* SVG line chart: x=sample index, y=rtt_ms. DEAD probes render as red dots
     along the x-axis and break the line so gaps are visible. */
  const W = 560, H = 96;
  const M = { t: 10, r: 12, b: 18, l: 40 };
  const plotW = W - M.l - M.r;
  const plotH = H - M.t - M.b;

  if (!rows || rows.length < 2) {
    return `<div class="chart-empty">collecting data\u2026</div>`;
  }
  const alive = rows.filter(r => r.status === 'ALIVE' && r.rtt_ms != null);
  if (!alive.length) {
    return `<div class="chart-empty">no live probes in window</div>`;
  }
  const rtts = alive.map(r => r.rtt_ms);
  let lo = Math.min(...rtts), hi = Math.max(...rtts);
  if (hi - lo < 1) { lo = Math.max(0, lo - 1); hi = hi + 1; }  /* always show some range */
  const span = hi - lo;
  const mid = (lo + hi) / 2;
  const n = rows.length;
  const xAt = i => M.l + (i / (n - 1)) * plotW;
  const yAt = v => M.t + plotH - ((v - lo) / span) * plotH;

  // split into live segments, breaking on DEAD
  const segs = [];
  let cur = [];
  rows.forEach((r, i) => {
    if (r.status === 'ALIVE' && r.rtt_ms != null) {
      cur.push([xAt(i), yAt(r.rtt_ms)]);
    } else {
      if (cur.length > 1) segs.push(cur);
      cur = [];
    }
  });
  if (cur.length > 1) segs.push(cur);
  if (!segs.length && cur.length === 1) segs.push([cur[0], [cur[0][0] + 0.5, cur[0][1]]]);

  const fmtPoint = p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`;
  const linePath = segs.map(s => 'M' + s.map(fmtPoint).join(' L')).join(' ');
  const baseY = M.t + plotH;
  const areaPath = segs.map(s => {
    return 'M' + s[0][0].toFixed(1) + ',' + baseY.toFixed(1)
         + ' L' + s.map(fmtPoint).join(' L')
         + ' L' + s[s.length-1][0].toFixed(1) + ',' + baseY.toFixed(1) + ' Z';
  }).join(' ');

  const deadDots = rows.map((r, i) => {
    if (r.status === 'ALIVE' && r.rtt_ms != null) return '';
    return `<circle class="dead-dot" cx="${xAt(i).toFixed(1)}" cy="${(M.t + plotH - 1.5).toFixed(1)}" r="2.2"/>`;
  }).join('');

  const yTicks = [
    { v: hi,  y: M.t + 3 },
    { v: mid, y: M.t + plotH / 2 },
    { v: lo,  y: baseY - 2 },
  ];
  const yTickMarkup = yTicks.map(t => `
    <line class="grid" x1="${M.l}" y1="${t.y}" x2="${M.l + plotW}" y2="${t.y}"/>
    <text class="tick" x="${M.l - 6}" y="${t.y + 3}" text-anchor="end">${t.v.toFixed(t.v >= 100 ? 0 : 1)}</text>`
  ).join('');

  const tStart = rows[0].ts ? String(rows[0].ts).slice(11, 19) : '';
  const tEnd   = rows[n-1].ts ? String(rows[n-1].ts).slice(11, 19) : '';
  const xTickMarkup = `
    <text class="tick" x="${M.l}"          y="${H - 4}" text-anchor="start">${tStart}</text>
    <text class="tick" x="${M.l + plotW}"  y="${H - 4}" text-anchor="end">${tEnd}</text>
    <text class="tick" x="${M.l + plotW/2}" y="${H - 4}" text-anchor="middle">RTT (ms)</text>`;

  return `<svg class="rtt-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line class="axis" x1="${M.l}" y1="${M.t}" x2="${M.l}" y2="${baseY}"/>
    <line class="axis" x1="${M.l}" y1="${baseY}" x2="${M.l + plotW}" y2="${baseY}"/>
    ${yTickMarkup}
    <path class="area" d="${areaPath}"/>
    <path class="line" d="${linePath}"/>
    ${deadDots}
    ${xTickMarkup}
  </svg>`;
}

function renderStrip(data) {
  const c = data.counts, t = data.totals, s = data.source;
  const avg = t.avg_uptime_24h;
  const strip = document.getElementById('stats-strip');
  const tile = (label, value, cls, sub) =>
    `<div class="tile"><div class="label">${label}</div>
      <div class="value ${cls||''}">${value}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ''}
    </div>`;
  strip.innerHTML =
      tile('monitor', c.monitor_targets, '', `${c.peers_total} peers total`)
    + tile('alive', c.alive, 'teal', c.dead ? `${c.dead} dead` : 'all ok')
    + tile('dead', c.dead, c.dead > 0 ? 'red' : 'dim', '—')
    + tile('maint', c.maintenance || 0, c.maintenance ? 'accent' : 'dim', '—')
    + tile('avg uptime 24h', avg == null ? '\u2014' : avg.toFixed(avg === 100 ? 0 : 2) + '%',
           avg == null ? 'dim' : (avg >= 99 ? 'teal' : (avg >= 95 ? 'accent' : 'red')), 'across peers')
    + tile('probes 24h', t.probes_24h.toLocaleString(), '', `${s.interval_seconds}s interval`)
    + tile('events 24h', t.events_last_24h, t.events_last_24h > 0 ? 'accent' : 'dim', 'monitor_up/_down')
    + tile('node · thresholds', esc(s.node_id.slice(0,14)) + '\u2026', 'accent',
           `\u2193${s.down_after} \u2191${s.up_after}  dwell ${s.flap_min_dwell_seconds}s`);
}

function renderPeers(data) {
  const grid = document.getElementById('peers-grid');
  if (!data.peers.length) {
    grid.innerHTML = `<div class="empty-peers">
      no monitor targets \u00b7 use <code>--add-peer &lt;node_id&gt; --permissions monitor</code>
    </div>`;
    return;
  }
  grid.innerHTML = data.peers.map(p => {
    const statusCls = p.in_maintenance ? 'maint'
                    : (p.status === 'ALIVE' ? 'alive'
                    : (p.status === 'DEAD'  ? 'dead' : 'unknown'));
    const statusText = p.in_maintenance ? 'MAINT' : p.status;
    const tags = (p.tags || []).length
      ? `<span class="tags">${esc(p.tags.join(' · '))}</span>`
      : '';
    const u = p.uptime;
    const cell = (key, v) =>
      `<div class="cell"><div class="n ${pctClass(v)}">${fmtPct(v)}</div><div class="l">${key}</div></div>`;

    const hourly = (p.hourly_24h || []).map(v =>
      `<div class="bar ${bucketClass(v)}" title="${v == null ? 'no data' : v.toFixed(1)+'%'}"></div>`
    ).join('') || Array.from({length:24}, () => `<div class="bar n"></div>`).join('');

    const maintBadge = p.in_maintenance
      ? `<div class="maint-badge">\u25d0 maintenance \u00b7 ends ${fmtTime(p.maintenance_end)}</div>`
      : '';

    const rttNow = p.rtt?.current;
    const rtt = p.rtt || {};
    const rttCell = (k, v) =>
      `<div class="cell"><span class="k">${k}</span><span class="v">${fmtMs(v)}</span></div>`;
    const rttGrid = `
      <div class="rtt-grid">
        ${rttCell('now',       rttNow)}
        ${rttCell('min 24h',   rtt.min_24h)}
        ${rttCell('max 24h',   rtt.max_24h)}
        ${rttCell('avg 24h',   rtt.avg_24h)}
      </div>`;

    const failCls = p.consecutive_failures > 0 ? 'warn' : '';
    const dead24 = rtt.dead_24h || 0;
    const probes24 = rtt.probes_24h || 0;
    return `<article class="peer-card ${statusCls}">
      <div class="peer-head">
        <span class="alias">${esc(p.alias || '—')}</span>
        ${tags}
        <span class="status ${statusCls}">\u25cf ${statusText}</span>
      </div>
      <div class="peer-nid">${esc(p.node_id)}</div>
      ${maintBadge}
      <div class="uptime-row">
        ${cell('1h',  u['1h'])}
        ${cell('24h', u['24h'])}
        ${cell('7d',  u['7d'])}
        ${cell('30d', u['30d'])}
      </div>
      <div>
        <div class="hourly">${hourly}</div>
        <div class="hourly-legend"><span>24h ago</span><span>now</span></div>
      </div>
      <div class="peer-metrics">
        <div class="rtt-chart-wrap">${rttChart(p.sparkline)}</div>
        ${rttGrid}
      </div>
      <div class="peer-foot">
        <span><span class="k">last seen</span><span class="v">${fmtTime(p.last_seen)}</span></span>
        <span><span class="k">rel</span><span class="v">${fmtRel(p.last_seen)}</span></span>
        <span><span class="k">fail streak</span><span class="v ${failCls}">${p.consecutive_failures}</span></span>
        <span><span class="k">probes 24h</span><span class="v">${probes24.toLocaleString()}</span></span>
        <span><span class="k">dead 24h</span><span class="v ${dead24 > 0 ? 'warn' : ''}">${dead24.toLocaleString()}</span></span>
      </div>
    </article>`;
  }).join('');
}

function renderEvents(data) {
  const tbody = document.getElementById('events-rows');
  if (!data.events || !data.events.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6">no monitor_up / monitor_down events yet</td></tr>`;
    return;
  }
  tbody.innerHTML = data.events.map(e => {
    const kind = e.type === 'monitor_up' ? 'up' : (e.type === 'monitor_down' ? 'down' : '');
    const reason = (e.data && e.data.reason) || '';
    const peer = e.peer_alias || (e.peer_node_id ? e.peer_node_id.slice(0,12) + '…' : '—');
    return `<tr>
      <td class="seq">#${e.seq}</td>
      <td>${fmtTime(e.timestamp)} <span class="seq">(${fmtRel(e.timestamp)})</span></td>
      <td class="evtype ${kind}">${esc(e.type)}</td>
      <td>${esc(peer)}</td>
      <td>${(e.data && e.data.consecutive_count) ?? '\u2014'}</td>
      <td>${esc(reason) || '<span class="seq">—</span>'}</td>
    </tr>`;
  }).join('');
}

function render(data) {
  document.getElementById('clock').textContent =
    new Date(data.now).toLocaleString([], {hour12:false});
  renderStrip(data);
  renderPeers(data);
  renderEvents(data);
}

async function refresh() {
  try {
    const r = await fetch('/status.json', {cache:'no-store'});
    if (!r.ok) throw new Error('status ' + r.status);
    render(await r.json());
  } catch (e) {
    document.getElementById('stats-strip').innerHTML =
      `<div class="tile"><div class="label">status</div><div class="value red">\u25cf disconnected</div><div class="sub">${esc(e.message)}</div></div>`;
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def _render_html() -> bytes:
    return _HTML_TEMPLATE.replace("__BANNER__", ASCII_BANNER).encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP server (stdlib, threaded; no new dependency)
# ---------------------------------------------------------------------------

def _parse_bind(value: str) -> tuple[Optional[str], Optional[int]]:
    """Parse ``host:port`` (e.g. ``127.0.0.1:8080``). Empty string → disabled."""
    v = (value or "").strip()
    if not v:
        return None, None
    host, sep, port = v.rpartition(":")
    if not sep or not host or not port:
        raise ValueError(
            f"invalid status-bind '{value}' — expected host:port or empty to disable"
        )
    return host, int(port)


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    # ``engine`` is patched onto the per-server subclass in StatusPageServer.start().
    engine: "MonitorEngine | None" = None

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("[statuspage] {} — {}", self.address_string(), fmt % args)

    def _send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        engine = type(self).engine
        if engine is None:
            self.send_error(503, "engine not ready")
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._send_html(_render_html())
                return
            if path == "/status.json":
                self._send_json(build_dashboard_snapshot(engine))
                return
            if path == "/history.json":
                q = parse_qs(parsed.query)
                node_id = (q.get("node_id") or [""])[0]
                if not node_id:
                    self._send_json({"error": "node_id required"}, status=400)
                    return
                hours = min(int((q.get("hours") or ["24"])[0]), 720)  # cap at 30 days
                rows = engine.history.recent_rows(node_id, hours=hours)
                self._send_json(
                    [
                        {
                            "ts": r.ts.isoformat(),
                            "rtt_ms": r.rtt_ms,
                            "status": r.status.value,
                        }
                        for r in rows
                    ]
                )
                return
            self.send_error(404, "not found")
        except Exception as exc:
            logger.error("[statuspage] handler error: {}", exc)
            try:
                self.send_error(500, str(exc))
            except Exception:  # noqa: BLE001 S110
                pass  # double-fault: error handler itself failed


class StatusPageServer:
    """Thin wrapper around ``http.server.ThreadingHTTPServer`` running in a
    daemon thread. Exposes local-only HTTP (``127.0.0.1:8080`` by default).

    No peer traffic touches this surface — ``/status.json`` is built from
    in-process state + local SQLite + the peers cache. The cross-device
    equivalent is the ``panic-monitor/status/0`` ALPN, not HTTP.
    """

    def __init__(self, engine: "MonitorEngine", bind: str = "127.0.0.1:8080") -> None:
        self._engine = engine
        self._bind_raw = bind
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        host, port = _parse_bind(self._bind_raw)
        if host is None or port is None:
            logger.info("[statuspage] disabled (--status-bind is empty)")
            return
        engine = self._engine

        class BoundHandler(_DashboardHandler):
            pass

        BoundHandler.engine = engine
        try:
            self._server = http.server.ThreadingHTTPServer((host, port), BoundHandler)
            self._server.request_queue_size = 8  # cap pending connections
            self._server.timeout = 30  # drop idle connections after 30s
        except OSError as exc:
            logger.error("[statuspage] bind {}:{} failed: {}", host, port, exc)
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="panic-monitor-statuspage",
            daemon=True,
        )
        self._thread.start()
        logger.info("[statuspage] serving dashboard at http://{}:{}/", host, port)

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:
                logger.debug("[statuspage] shutdown error: {}", exc)
        self._server = None
        self._thread = None
