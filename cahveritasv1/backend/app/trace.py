"""CAH VERITAS — per-run agent trace bus.

Powers the live Agent Stream in the UI: each AAOSA agent posts start/finish
events with elapsed timings; the SSE endpoint drains the bus as it fills, so
users see the network working the moment it starts (perceived latency).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TraceEvent:
    seq: int
    ts: float
    agent: str
    event: str          # start | finish | info | error
    detail: str = ""
    elapsed_ms: Optional[int] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "agent": self.agent,
                "event": self.event, "detail": self.detail,
                "elapsed_ms": self.elapsed_ms, "data": self.data}


class TraceBus:
    def __init__(self) -> None:
        self._events: List[TraceEvent] = []
        self._seq = 0
        self._starts: Dict[str, float] = {}
        self._cond = asyncio.Condition()
        self.done = False

    async def emit(self, agent: str, event: str, detail: str = "",
                   data: Optional[Dict[str, Any]] = None) -> None:
        async with self._cond:
            self._seq += 1
            elapsed = None
            now = time.time()
            if event == "start":
                self._starts[agent] = now
            elif event in ("finish", "error") and agent in self._starts:
                elapsed = int((now - self._starts.pop(agent)) * 1000)
            self._events.append(TraceEvent(self._seq, now, agent, event, detail,
                                           elapsed, data or {}))
            self._cond.notify_all()

    async def close(self) -> None:
        async with self._cond:
            self.done = True
            self._cond.notify_all()

    async def drain(self, after_seq: int = 0, timeout: float = 25.0
                    ) -> List[TraceEvent]:
        """Block until there is something newer than after_seq (or done)."""
        async with self._cond:
            if not self.done and not any(e.seq > after_seq for e in self._events):
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
            return [e for e in self._events if e.seq > after_seq]

    def snapshot(self) -> List[Dict[str, Any]]:
        """Every event so far — persisted with the run so a refreshed page can
        show what the agents did."""
        return [e.as_dict() for e in self._events]

    def timings(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self._events:
            if e.event == "finish" and e.elapsed_ms is not None:
                out[e.agent] = out.get(e.agent, 0) + e.elapsed_ms
        return out


_BUSES: Dict[str, TraceBus] = {}


def bus_for(run_id: str) -> TraceBus:
    if run_id not in _BUSES:
        _BUSES[run_id] = TraceBus()
    return _BUSES[run_id]


def drop(run_id: str) -> None:
    _BUSES.pop(run_id, None)
