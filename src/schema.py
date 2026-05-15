from __future__ import annotations

import enum
from collections import deque
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

HISTORY_MAXLEN = 100


class PeerStatus(str, enum.Enum):
    ALIVE = "ALIVE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


class PeerEntry(BaseModel):
    """Runtime-only view of a peer we actively monitor (NodeID + alias).

    The authoritative peer record lives in the trust log; this struct is just
    the slice the engine needs to drive a heartbeat probe.
    """

    node_id: str
    alias: Optional[str] = None


class LatencyRecord(BaseModel):
    """Single heartbeat measurement."""

    timestamp: datetime
    rtt_ms: Optional[float] = None
    status: PeerStatus


class PeerState:
    """
    Runtime state for a monitored peer.

    Not a Pydantic model — holds mutable deque and is never serialized.
    """

    __slots__ = (
        "entry",
        "latency_history",
        "last_seen",
        "consecutive_failures",
        "consecutive_successes",
        "current_status",
        "last_fail_reason",
        "cached_node_addr",
    )

    def __init__(self, entry: PeerEntry) -> None:
        self.entry: PeerEntry = entry
        self.latency_history: deque[LatencyRecord] = deque(maxlen=HISTORY_MAXLEN)
        self.last_seen: Optional[datetime] = None
        self.consecutive_failures: int = 0
        self.consecutive_successes: int = 0
        self.current_status: PeerStatus = PeerStatus.UNKNOWN
        self.last_fail_reason: Optional[str] = None
        self.cached_node_addr: object | None = None  # iroh.NodeAddr, cached to avoid FFI churn
