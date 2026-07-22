"""Colonization tab for EliteOps.

Two halves:
  * Active construction — reads the journal's ColonisationConstructionDepot (per-commodity
    provided/required, overall progress) and sources the still-needed commodities from
    Spansh markets, so you know what to haul and where to buy it.
  * Where to expand — finds nearby UNPOPULATED systems via Spansh /systems/search, scores
    them for colonization (valuable bodies, exobio, proximity to populated space), and
    returns coords for a spatial heatmap of the best spots around you.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import threading
from typing import Any

from .state import DEFAULT_JOURNAL_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import spansh_client  # noqa: E402


def _clean(name: str) -> str:
    s = str(name or "")
    if s.startswith("$") and s.endswith(";"):
        s = s[1:-1]
        if s.endswith("_name"):
            s = s[:-5]
    return s


class ColonyEngine:
    def __init__(self, state) -> None:
        self.state = state
        self._lock = threading.Lock()
        self._construction: dict | None = None
        self._locations: dict[Any, dict] = {}     # MarketID -> {station, system}
        self._sources: dict[str, dict] = {}       # commodity -> {status, stops, error}
        self._cand: dict = {"status": "idle", "systems": [], "error": ""}
        self._seed_construction(getattr(state, "dir", DEFAULT_JOURNAL_DIR))
        state.add_journal_listener(self.on_journal)

    def _seed_construction(self, journal_dir: str) -> None:
        """Scan journals newest-first for the latest ColonisationConstructionDepot (your
        build site may be from a past session), stopping at the first file that has one
        and taking that file's Docked events for the MarketID->location."""
        try:
            files = sorted(glob.glob(os.path.join(journal_dir, "Journal.*.log")),
                           key=os.path.getmtime, reverse=True)[:40]
        except OSError:
            return
        for path in files:
            last_depot, locations = None, {}
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "Docked" not in line and "ColonisationConstructionDepot" not in line:
                            continue
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        ev = e.get("event")
                        if ev == "Docked" and e.get("MarketID") is not None:
                            locations[e["MarketID"]] = {"station": e.get("StationName"),
                                                        "system": e.get("StarSystem")}
                        elif ev == "ColonisationConstructionDepot":
                            last_depot = e
            except OSError:
                continue
            if last_depot:
                self._locations.update(locations)
                self._apply_depot(last_depot)
                return

    # --- journal ------------------------------------------------------------
    def on_journal(self, e: dict) -> None:
        ev = e.get("event")
        with self._lock:
            if ev == "Docked":
                mid = e.get("MarketID")
                if mid is not None:
                    self._locations[mid] = {"station": e.get("StationName"),
                                            "system": e.get("StarSystem")}
            elif ev == "ColonisationConstructionDepot":
                self._apply_depot(e)

    def _apply_depot(self, e: dict) -> None:
        resources = []
        total_req = total_prov = 0
        for r in e.get("ResourcesRequired") or []:
            req = int(r.get("RequiredAmount") or 0)
            prov = int(r.get("ProvidedAmount") or 0)
            total_req += req
            total_prov += prov
            resources.append({
                "name": r.get("Name_Localised") or _clean(r.get("Name")),
                "required": req, "provided": prov,
                "remaining": max(0, req - prov),
                "payment": r.get("Payment"),
                "pct": round(prov / req, 4) if req else 1.0,
            })
        resources.sort(key=lambda x: -x["remaining"])
        mid = e.get("MarketID")
        loc = self._locations.get(mid, {})
        snap = self.state.snapshot()
        station = loc.get("station") or snap.get("station")
        if station:
            station = station.replace("$EXT_PANEL_ColonisationShip;", "").strip() or "Colonisation Ship"
        self._construction = {
            "market_id": mid,
            "system": loc.get("system") or snap.get("system"),
            "station": station,
            "progress": e.get("ConstructionProgress"),
            "complete": bool(e.get("ConstructionComplete")),
            "failed": bool(e.get("ConstructionFailed")),
            "updated": e.get("timestamp"),
            "resources": resources,
            "total_required": total_req, "total_provided": total_prov,
            "total_remaining": max(0, total_req - total_prov),
        }

    # --- commodity sourcing (Spansh, on demand) ----------------------------
    def source(self, commodity: str) -> None:
        commodity = str(commodity or "").strip()
        if not commodity:
            return
        with self._lock:
            self._sources[commodity] = {"status": "searching", "stops": [], "error": ""}
        threading.Thread(target=self._run_source, args=(commodity,),
                         name="eliteops-colony-source", daemon=True).start()

    def _run_source(self, commodity: str) -> None:
        try:
            ref = self.state.snapshot().get("system")
            if not ref:
                raise ValueError("No reference system.")
            amount = 1000
            with self._lock:
                if self._construction:
                    for r in self._construction["resources"]:
                        if r["name"] == commodity:
                            amount = max(1, r["remaining"] or 1000)
            srcs = spansh_client.find_commodity_sources(ref, commodity, amount,
                                                        large_pad_only=True, limit=5, timeout=35.0)
            stops = [{"system": s.get("system"), "station": s.get("station"),
                      "distance_ly": s.get("distance_ly"), "distance_ls": s.get("distance_ls"),
                      "buy_price": s.get("buy_price"), "supply": s.get("supply")} for s in srcs]
            with self._lock:
                self._sources[commodity] = {"status": "ready", "stops": stops, "error": "", "reference": ref}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._sources[commodity] = {"status": "error", "stops": [], "error": str(exc)[:140]}

    # --- candidate systems (Spansh, on demand) -----------------------------
    def find_candidates(self, radius: float = 40.0) -> None:
        with self._lock:
            self._cand = {"status": "searching", "systems": [], "error": ""}
        threading.Thread(target=self._run_candidates, args=(radius,),
                         name="eliteops-colony-cand", daemon=True).start()

    def _run_candidates(self, radius: float) -> None:
        try:
            snap = self.state.snapshot()
            ref = snap.get("system")
            if not ref:
                raise ValueError("No reference system — jump in-game first.")
            body = {"filters": {"distance": {"min": "0", "max": str(radius)},
                                "population": {"comparison": "<=>", "value": [0, 0]}},
                    "sort": [{"distance": {"direction": "asc"}}],
                    "reference_system": ref, "size": 60}
            data = spansh_client._jpost("/systems/search", body, timeout=35.0)
            results = (data.get("results") if isinstance(data, dict) else []) or []
            ref_xyz = None
            systems = []
            for s in results:
                if s.get("needs_permit"):
                    continue
                value = int(s.get("estimated_scan_value") or 0) + int(s.get("estimated_mapping_value") or 0)
                exobio = int(s.get("landmark_value") or 0)
                bodies = int(s.get("body_count") or 0)
                score = value + exobio + bodies * 20000
                if s.get("name") == ref:
                    ref_xyz = [s.get("x"), s.get("y"), s.get("z")]
                why = []
                if exobio >= 1_000_000:
                    why.append("rich exobiology")
                if value >= 2_000_000:
                    why.append("high-value bodies")
                if bodies >= 10:
                    why.append(f"{bodies} bodies")
                systems.append({
                    "name": s.get("name"), "distance_ly": s.get("distance"),
                    "bodies": bodies, "value": value, "exobio": exobio, "score": score,
                    "nearest_populated_ly": s.get("nearest_populated_distance"),
                    "x": s.get("x"), "y": s.get("y"), "z": s.get("z"),
                    "why": why,
                })
            systems.sort(key=lambda x: -x["score"])
            with self._lock:
                self._cand = {"status": "ready", "systems": systems[:40], "error": "",
                              "reference": ref, "ref_xyz": ref_xyz,
                              "ref_system_xyz": [snap.get("x"), snap.get("y"), snap.get("z")]}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._cand = {"status": "error", "systems": [], "error": str(exc)[:140]}

    # --- snapshot ----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "construction": self._construction,
                "sources": self._sources,
                "candidates": self._cand,
            }
