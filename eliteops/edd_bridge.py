"""Optional EDDiscovery bridge (Tier 4) — read-only, graceful fallback.

EliteOps needs no EDDiscovery. But if EDD is installed, its EDDUser.sqlite holds the
commander's FULL cross-session journal history (every session, not just the live log
EliteOps tails), which lets us surface lifetime/career stats. This reads that DB
read-only with sqlite3 (stdlib). If the DB is missing or unreadable, every method
degrades to "not present" and the rest of EliteOps is unaffected.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

_CANDIDATES = [
    os.environ.get("ELITEOPS_EDD_DB", ""),
    r"E:\EDDiscovery\Data\EDDUser.sqlite",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "EDDiscovery", "EDDUser.sqlite"),
    os.path.join(os.environ.get("APPDATA", ""), "EDDiscovery", "EDDUser.sqlite"),
]

# events we aggregate for career stats (keeps the scan narrow and fast)
_CAREER_EVENTS = ("FSDJump", "CarrierJump", "Scan", "SAAScanComplete", "CodexEntry")


class EddBridge:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or self._find()
        self._lock = threading.Lock()
        self._career_cache: dict | None = None
        self._career_mtime: float | None = None

    @staticmethod
    def _find() -> str | None:
        for cand in _CANDIDATES:
            if cand and os.path.isfile(cand):
                return cand
        return None

    def available(self) -> bool:
        return bool(self.path and os.path.isfile(self.path))

    def _connect(self) -> sqlite3.Connection:
        # read-only; short timeout so a busy (running-EDD) DB never blocks us
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2.0)

    def _edd_running(self) -> bool:
        """Best-effort: EDD keeps a -wal sidecar hot while running (no process spawn)."""
        if not self.path:
            return False
        try:
            wal = self.path + "-wal"
            if os.path.isfile(wal) and os.path.getsize(wal) > 0:
                import time
                return (time.time() - os.path.getmtime(wal)) < 90
        except OSError:
            pass
        return False

    def status(self) -> dict:
        if not self.available():
            return {"present": False, "readable": False, "path": self.path,
                    "message": "EDDiscovery database not found — using live journal only."}
        try:
            con = self._connect()
            try:
                cur = con.cursor()
                count = cur.execute("SELECT COUNT(*) FROM JournalEntries").fetchone()[0]
                last = cur.execute("SELECT MAX(EventTime) FROM JournalEntries").fetchone()[0]
                cmdr_row = cur.execute("SELECT Name FROM Commanders LIMIT 1").fetchone()
            finally:
                con.close()
            return {"present": True, "readable": True, "path": self.path,
                    "entries": count, "last_event": last,
                    "commander": cmdr_row[0] if cmdr_row else None,
                    "running": self._edd_running(),
                    "message": f"EDD history connected — {count:,} events."}
        except sqlite3.Error as exc:
            return {"present": True, "readable": False, "path": self.path,
                    "running": self._edd_running(),
                    "message": f"EDD database is busy/locked ({exc}); using live journal only."}

    def career(self) -> dict | None:
        """Lifetime aggregates from the full journal history. Cached by DB mtime."""
        if not self.available():
            return None
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return None
        with self._lock:
            if self._career_cache is not None and self._career_mtime == mtime:
                return self._career_cache
        try:
            data = self._compute_career()
        except sqlite3.Error:
            return None
        with self._lock:
            self._career_cache = data
            self._career_mtime = mtime
        return data

    def _compute_career(self) -> dict:
        systems: set = set()
        distance = 0.0
        scans = 0
        first_disc: set = set()
        first_map = 0
        codex_new = 0
        con = self._connect()
        try:
            placeholders = ",".join("?" for _ in _CAREER_EVENTS)
            q = f"SELECT EventType, EventData FROM JournalEntries WHERE EventType IN ({placeholders})"
            for et, ed in con.execute(q, _CAREER_EVENTS):
                try:
                    d = json.loads(ed)
                except (ValueError, TypeError):
                    continue
                if et in ("FSDJump", "CarrierJump"):
                    if d.get("StarSystem"):
                        systems.add(d["StarSystem"])
                    distance += float(d.get("JumpDist") or 0)
                elif et == "Scan":
                    scans += 1
                    if d.get("WasDiscovered") is False:
                        first_disc.add((d.get("SystemAddress"), d.get("BodyID")))
                elif et == "SAAScanComplete":
                    first_map += 1
                elif et == "CodexEntry":
                    if d.get("IsNewEntry"):
                        codex_new += 1
        finally:
            con.close()
        return {"systems_visited": len(systems), "ly_traveled": round(distance),
                "bodies_scanned": scans, "first_discovered": len(first_disc),
                "bodies_mapped": first_map, "codex_new": codex_new}

    def snapshot(self) -> dict:
        s = self.status()
        if s.get("readable"):
            s["career"] = self.career()
        return s
