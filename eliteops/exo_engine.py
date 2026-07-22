"""Exobiology engine for EliteOps — Spansh route + live organic tracking.

Two halves:
  * Route: Spansh /exobiology/route (via vendored spansh_client.exobiology_route) —
    the highest-value bio systems near you, ordered, with per-species credit values.
  * Live: tracks the current system's biological signals (SAASignalsFound), your
    sampling progress per species (ScanOrganic Log->Sample->Analyse = 3 scans), new
    codex entries, and unsold bio value at risk vs sold (SellOrganicData).

Species credit values come from the bundled Canonn/Vista-Genomics catalog.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import spansh_client  # noqa: E402
from exobiology_catalog import DEFAULT_CATALOG as _CATALOG  # noqa: E402


def _loc(entry: dict, key: str) -> str:
    return entry.get(key + "_Localised") or entry.get(key) or ""


def _norm(v: Any) -> str:
    return "".join(c for c in str(v or "").lower() if c.isalnum())


class ExoEngine:
    def __init__(self, state) -> None:
        self.state = state
        self.cat = _CATALOG
        self._lock = threading.Lock()
        # route
        self._route: dict | None = None
        self._status = "idle"
        self._error = ""
        self._params: dict = {}
        # live
        self._system: str | None = None
        self._system_addr: Any = None
        self._bodies: dict[Any, dict] = {}     # bodyid -> {name, count, genuses:set}
        self._species: dict[str, dict] = {}    # session species -> progress/value
        self._sold_value = 0
        state.add_journal_listener(self.on_journal)

    # --- route generation ---------------------------------------------------
    def generate(self, params: dict) -> None:
        with self._lock:
            self._status = "generating"
            self._error = ""
            self._params = dict(params)
        threading.Thread(target=self._run_generate, args=(params,),
                         name="eliteops-exo-gen", daemon=True).start()

    def _run_generate(self, params: dict) -> None:
        try:
            route = spansh_client.exobiology_route(
                from_system=params["from_system"],
                jump_range=float(params.get("jump_range") or 50),
                radius=float(params.get("radius") or 100),
                min_value=int(params.get("min_value") or 0),
                max_results=int(params.get("max_results") or 25),
                loop=bool(params.get("loop", False)),
                use_mapping_value=bool(params.get("use_mapping_value", True)),
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._status = "error"
                self._error = str(exc)
            return
        with self._lock:
            self._route = route
            self._status = "ready"
            self._error = ""

    # --- live journal tracking ---------------------------------------------
    def on_journal(self, e: dict) -> None:
        ev = e.get("event")
        with self._lock:
            if ev in ("FSDJump", "CarrierJump", "Location"):
                addr = e.get("SystemAddress")
                if addr != self._system_addr:
                    self._system = e.get("StarSystem")
                    self._system_addr = addr
                    self._bodies = {}  # current-system signal view resets per system
            elif ev == "SAASignalsFound":
                self._saa(e)
            elif ev == "ScanOrganic":
                self._scan_organic(e)
            elif ev == "CodexEntry" and e.get("IsNewEntry"):
                self._mark_new(e)
            elif ev in ("SellOrganicData", "MultiSellOrganicData"):
                self._sold_value += sum(int(b.get("Value") or 0) + int(b.get("Bonus") or 0)
                                        for b in (e.get("BioData") or []))
                for sp in self._species.values():
                    if sp["complete"]:
                        sp["sold"] = True

    def _saa(self, e: dict) -> None:
        if not any("biolog" in str(s.get("Type", "")).lower() for s in (e.get("Signals") or [])):
            return
        bid = e.get("BodyID")
        count = 0
        for s in e.get("Signals") or []:
            if "biolog" in str(s.get("Type", "")).lower():
                count = s.get("Count", count)
        genuses = [_loc(g, "Genus") for g in (e.get("Genuses") or []) if _loc(g, "Genus")]
        self._bodies[bid] = {"name": e.get("BodyName"), "count": count, "genuses": genuses}

    def _scan_organic(self, e: dict) -> None:
        species = _loc(e, "Species")
        if not species:
            return
        key = f"{e.get('SystemAddress')}:{e.get('Body')}:{_norm(species)}"
        sp = self._species.get(key)
        if not sp:
            match = self.cat.resolve(_loc(e, "Variant"), species)
            sp = {"genus": _loc(e, "Genus"), "species": species,
                  "system": self._system, "system_addr": e.get("SystemAddress"),
                  "scans": 0, "complete": False, "sold": False, "new": False,
                  "value": match.base_value if match else None}
            self._species[key] = sp
        sp["scans"] = min(3, sp["scans"] + 1)
        if str(e.get("ScanType")) == "Analyse":
            sp["complete"] = True
            sp["scans"] = 3

    def _mark_new(self, e: dict) -> None:
        name = _loc(e, "Name")
        n = _norm(name)
        for sp in self._species.values():
            if _norm(sp["species"]) == n or _norm(sp["species"]) in n or n in _norm(sp["species"]):
                sp["new"] = True

    # --- value helpers ------------------------------------------------------
    def _species_value(self, sp: dict) -> int:
        base = sp.get("value") or 0
        return base * self.cat.first_logged_multiplier if sp.get("new") else base

    def _body_potential(self, genuses: list[str]) -> int:
        total = 0
        for g in genuses:
            _lo, hi = self.cat.value_range_for_genus(g)
            if hi:
                total += hi
        return total

    # --- snapshot -----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            # current-system bio bodies (from SAA)
            bodies = []
            for bid, b in self._bodies.items():
                bodies.append({
                    "body": b["name"], "count": b["count"], "genuses": b["genuses"],
                    "potential_value": self._body_potential(b["genuses"]),
                })
            bodies.sort(key=lambda x: -x["potential_value"])

            # sampling progress for the CURRENT system
            here = [sp for sp in self._species.values() if sp.get("system_addr") == self._system_addr]
            sampling = sorted(({
                "genus": sp["genus"], "species": sp["species"], "scans": sp["scans"],
                "complete": sp["complete"], "new": sp["new"],
                "value": self._species_value(sp),
            } for sp in here), key=lambda x: (x["complete"], -(x["value"] or 0)))

            # session totals
            completed = [sp for sp in self._species.values() if sp["complete"]]
            unsold = [sp for sp in completed if not sp.get("sold")]
            unsold_value = sum(self._species_value(sp) for sp in unsold)
            session = {
                "species_sampled": len(completed),
                "unsold_value": unsold_value,
                "unsold_count": len(unsold),
                "sold_value": self._sold_value,
                "new_codex": sum(1 for sp in self._species.values() if sp["new"]),
            }
            return {
                "status": self._status, "error": self._error, "params": self._params,
                "route": self._route, "system": self._system,
                "bodies": bodies, "sampling": sampling, "session": session,
            }

    def prefill(self) -> dict:
        snap = self.state.snapshot()
        jr = snap.get("jump_range")
        return {"from_system": snap.get("system") or "",
                "jump_range": round(jr, 2) if isinstance(jr, (int, float)) else 50}

    # --- persistence --------------------------------------------------------
    def persist(self) -> dict:
        with self._lock:
            return {"route": self._route, "status": self._status, "params": self._params}

    def restore(self, data: dict | None) -> None:
        if not data or not data.get("route"):
            return
        with self._lock:
            self._route = data["route"]
            self._status = data.get("status") or "ready"
            self._params = data.get("params") or {}
