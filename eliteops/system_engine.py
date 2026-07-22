"""Interactive system-scan model for EliteOps — an EDD-style body view.

Builds the current system's body tree from Scan events (parent hierarchy, type,
value, distance, rings, landable/terraform, bio signals, discovered/mapped status),
so the web UI can render a scan panel like EDDiscovery's. Values reuse the firsts.py
exploration formula. Standard library.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import firsts  # noqa: E402


def _category(planet_class: str, star: bool, star_type: str) -> str:
    if star:
        if star_type in ("N", "H"):
            return "compact"
        if star_type.startswith("D"):
            return "whitedwarf"
        return "star"
    pc = planet_class
    if pc == "Earthlike body":
        return "elw"
    if pc == "Water world":
        return "ww"
    if pc == "Ammonia world":
        return "aw"
    if "gas giant" in pc.lower() or "giant" in pc.lower():
        return "gg"
    if pc == "Metal rich body":
        return "metalrich"
    if pc == "High metal content body":
        return "hmc"
    if pc == "Icy body":
        return "icy"
    if "rocky" in pc.lower():
        return "rocky"
    return "other"


class SystemEngine:
    def __init__(self, state) -> None:
        self._lock = threading.Lock()
        self._system: str | None = None
        self._addr: Any = None
        self._body_count: int | None = None
        self._bodies: dict[int, dict] = {}
        state.add_journal_listener(self.on_journal)

    def on_journal(self, e: dict) -> None:
        ev = e.get("event")
        with self._lock:
            if ev in ("FSDJump", "CarrierJump", "Location"):
                if e.get("SystemAddress") != self._addr:
                    self._system = e.get("StarSystem")
                    self._addr = e.get("SystemAddress")
                    self._body_count = None
                    self._bodies = {}
            elif ev == "FSSDiscoveryScan":
                if e.get("SystemAddress") in (None, self._addr):
                    self._body_count = e.get("BodyCount")
            elif ev == "Scan":
                self._scan(e)
            elif ev == "SAASignalsFound":
                self._bio(e)
            elif ev == "SAAScanComplete":
                b = self._bodies.get(e.get("BodyID"))
                if b:
                    b["mapped"] = True
                    b["mapped_by_me"] = True

    def _scan(self, e: dict) -> None:
        bid = e.get("BodyID")
        if bid is None:
            return
        if not e.get("StarType") and not e.get("PlanetClass"):
            return  # belt clusters / non-body scans — clutter, not real bodies
        if e.get("SystemAddress") not in (None, self._addr):
            self._system = e.get("StarSystem")
            self._addr = e.get("SystemAddress")
            self._bodies = {}
        star = bool(e.get("StarType"))
        planet_class = e.get("PlanetClass") or ""
        star_type = e.get("StarType") or ""
        discovered = bool(e.get("WasDiscovered", True))
        mapped = bool(e.get("WasMapped", False))
        mass = e.get("StellarMass") if star else e.get("MassEM")
        terraform = str(e.get("TerraformState") or "") in ("Terraformable", "Terraforming")
        # value via firsts formula
        value = None
        if mass is not None:
            if star:
                value = firsts.star_scan_value(star_type, mass, not discovered)
            elif planet_class:
                value = firsts.planet_claim_value(planet_class, terraform, mass,
                                                  not discovered, not mapped)
        # nearest Star/Planet parent for the tree
        parent = None
        for p in e.get("Parents") or []:
            for kind, pid in p.items():
                if kind in ("Star", "Planet"):
                    parent = pid
                    break
            if parent is not None:
                break
        rings = [r for r in (e.get("Rings") or []) if "Belt" not in str(r.get("Name", ""))]
        prev = self._bodies.get(bid, {})
        self._bodies[bid] = {
            "id": bid, "name": e.get("BodyName", ""), "parent": parent,
            "star": star, "star_type": star_type, "planet_class": planet_class,
            "category": _category(planet_class, star, star_type),
            "kind": (star_type + " star") if star else planet_class,
            "distance_ls": e.get("DistanceFromArrivalLS"),
            "value": value, "landable": bool(e.get("Landable", False)),
            "terraformable": terraform, "discovered": discovered,
            "mapped": mapped or prev.get("mapped_by_me", False),
            "mapped_by_me": prev.get("mapped_by_me", False),
            "bio_signals": prev.get("bio_signals", 0),
            "rings": len(rings), "atmosphere": e.get("Atmosphere") or "",
            "volcanism": e.get("Volcanism") or "",
            "gravity": e.get("SurfaceGravity"), "temperature": e.get("SurfaceTemperature"),
            "mass": mass, "radius": e.get("Radius"),
        }

    def _bio(self, e: dict) -> None:
        bid = e.get("BodyID")
        count = 0
        for s in e.get("Signals") or []:
            if "biolog" in str(s.get("Type", "")).lower():
                count = s.get("Count", count)
        b = self._bodies.get(bid)
        if b:
            b["bio_signals"] = count or b.get("bio_signals", 0)
        elif bid is not None:
            self._bodies[bid] = {"id": bid, "name": e.get("BodyName", ""), "parent": None,
                                 "bio_signals": count, "category": "other", "pending": True}

    # --- snapshot -----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            bodies = {bid: b for bid, b in self._bodies.items() if not b.get("pending")}
            # build ordered flat list with depth via parent tree
            children: dict[Any, list] = {}
            for b in bodies.values():
                children.setdefault(b.get("parent"), []).append(b)
            for lst in children.values():
                lst.sort(key=lambda x: (x.get("distance_ls") if x.get("distance_ls") is not None else 1e18, x["id"]))
            ordered: list[dict] = []

            def walk(pid, depth, guard):
                for b in children.get(pid, []):
                    if b["id"] in guard:
                        continue
                    guard.add(b["id"])
                    ordered.append({**b, "depth": depth})
                    walk(b["id"], depth + 1, guard)

            # roots = bodies whose parent isn't a scanned body; each body appears once
            ids = set(bodies)
            roots = sorted((b for b in bodies.values() if b.get("parent") not in ids),
                           key=lambda x: (x.get("distance_ls") or 0, x["id"]))
            guard: set = set()
            for r in roots:
                if r["id"] in guard:
                    continue
                guard.add(r["id"])
                ordered.append({**r, "depth": 0})
                walk(r["id"], 1, guard)

            scanned = len(bodies)
            total_value = sum(b.get("value") or 0 for b in bodies.values())
            named = [b for b in bodies.values() if b.get("name")]
            undiscovered = bool(named) and all(not b.get("discovered", True) for b in named)

            # "worth stopping for" — high value / notable, unmapped-first, top 6
            def _notable(b):
                return (b.get("category") in ("elw", "ww", "aw") or b.get("terraformable")
                        or (b.get("bio_signals") or 0) > 0 or (b.get("value") or 0) >= 300000)
            highlights = []
            for b in sorted((b for b in bodies.values() if _notable(b) and not b.get("star")),
                            key=lambda x: (x.get("mapped", False), -(x.get("value") or 0)))[:6]:
                tags = []
                if b["category"] == "elw":
                    tags.append("Earth-like")
                elif b["category"] == "ww":
                    tags.append("Water world")
                elif b["category"] == "aw":
                    tags.append("Ammonia world")
                if b.get("terraformable"):
                    tags.append("Terraformable")
                if (b.get("bio_signals") or 0) > 0:
                    tags.append(f"{b['bio_signals']} bio")
                highlights.append({
                    "name": b["name"], "category": b["category"], "value": b.get("value"),
                    "distance_ls": b.get("distance_ls"), "mapped": b.get("mapped", False),
                    "landable": b.get("landable", False), "tags": tags,
                })
            return {
                "system": self._system, "body_count": self._body_count,
                "scanned": scanned, "total_value": total_value,
                "undiscovered": undiscovered, "highlights": highlights, "bodies": ordered,
            }
