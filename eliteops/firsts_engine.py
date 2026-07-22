"""Firsts Radar engine for EliteOps — a near drop-in of the standalone firsts.py.

Subscribes to the shared EliteState journal listener (which replays the whole
current journal on the first poll, then streams new events), so the Radar always
reflects the system you're in: which bodies still offer a first discovery / map /
footfall, likely-first-logged bio, session tally, and estimated credit value.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import firsts  # noqa: E402


class FirstsEngine:
    def __init__(self, state) -> None:
        self.radar = firsts.Radar()
        self._lock = threading.Lock()
        state.add_journal_listener(self.on_journal)

    def on_journal(self, entry: dict[str, Any]) -> None:
        with self._lock:
            try:
                self.radar.apply(entry)
            except Exception:  # noqa: BLE001 - a bad event must not break polling
                pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            r = self.radar
            rows = []
            for b in r.rows():
                # category drives the row colour, matching the standalone radar
                if b.first_footfall:
                    cat = "footfall"
                elif b.first_discovery:
                    cat = "discovery"
                elif b.first_map:
                    cat = "map"
                else:
                    cat = "none"
                rows.append({
                    "name": b.name or "?",
                    "kind": b.kind or "?",
                    "distance_ls": b.distance_ls,
                    "bio": b.bio_signals or 0,
                    "value": b.value(),
                    "badges": b.badges(),
                    "category": cat,
                    "first_discovery": b.first_discovery,
                    "first_map": b.first_map,
                    "first_footfall": b.first_footfall,
                    "has_first": b.has_first,
                })
            available = sum(1 for b in r.bodies.values() if b.has_first)
            return {
                "system": r.system,
                "body_count": r.body_count,
                "scanned": len([b for b in r.bodies.values() if b.name]),
                "undiscovered": r.undiscovered_system,
                "available": available,
                "firsts_value": r.firsts_value(),
                "tally": dict(r.tally),
                "rows": rows,
            }
