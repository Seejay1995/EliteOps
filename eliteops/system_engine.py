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

from . import edsm_client  # noqa: E402


# raw surface-material grades (the 7×4 grid). G4 = rarest/most useful for engineering.
_RAW_GRADE = {
    "carbon": 1, "phosphorus": 1, "sulphur": 1, "iron": 1, "nickel": 1, "rhenium": 1, "lead": 1,
    "vanadium": 2, "chromium": 2, "manganese": 2, "zinc": 2, "germanium": 2, "arsenic": 2, "zirconium": 2,
    "niobium": 3, "molybdenum": 3, "cadmium": 3, "tin": 3, "tungsten": 3, "mercury": 3, "boron": 3,
    "yttrium": 4, "technetium": 4, "ruthenium": 4, "selenium": 4, "tellurium": 4, "polonium": 4, "antimony": 4,
}

# ring class -> what it's worth mining for (ties into the Mining tab)
_RING_HINT = {
    "icy": "Icy — Low Temp. Diamonds, Void Opals, Bromellite, Tritium",
    "rocky": "Rocky — Alexandrite, Monazite, Bromellite",
    "metalrich": "Metal-Rich — Painite, Platinum, core gems (Musgravite/Serendibite/Monazite)",
    "metalic": "Metallic — Platinum, Painite, Palladium",
}


def _ring_class(raw: str) -> str:
    return str(raw or "").replace("eRingClass_", "").lower()


def _clean(v: Any) -> str:
    s = str(v or "")
    if s.startswith("$") and s.endswith(";"):
        s = s[1:-1]
    return s.replace("Ring_Level_", "").replace("Resources", " Resources").strip()


# EDSM subType -> the PlanetClass string the value formula + _category expect
def _edsm_planet_class(sub: str) -> str:
    s = str(sub or "").lower()
    if "earth-like" in s:
        return "Earthlike body"
    if "water world" in s:
        return "Water world"
    if "ammonia world" in s:
        return "Ammonia world"
    if "water giant" in s:
        return "Water giant"
    if "gas giant" in s or "class i" in s or "class ii" in s or "class iii" in s \
            or "class iv" in s or "class v" in s or s.endswith("life"):
        for roman in ("v", "iv", "iii", "ii", "i"):
            if f"class {roman} " in s or s.endswith(f"class {roman}"):
                return f"Sudarsky class {roman.upper()} gas giant"
        return "Sudarsky class I gas giant"
    if "metal-rich" in s or "metal rich" in s:
        return "Metal rich body"
    if "high metal content" in s:
        return "High metal content body"
    if "rocky ice" in s:
        return "Rocky ice body"
    if "rocky" in s:
        return "Rocky body"
    if "icy" in s:
        return "Icy body"
    return "Rocky body"


def _edsm_star_type(sub: str) -> str:
    s = str(sub or "").lower()
    if "neutron" in s:
        return "N"
    if "black hole" in s:
        return "H"
    if "white dwarf" in s or s.startswith("d"):
        return "D"
    first = str(sub or "").strip()[:1].upper()
    return first if first.isalpha() else "G"


_EDSM_RING = {"metal rich": "metalrich", "metalrich": "metalrich", "metallic": "metalic",
              "metalic": "metalic", "icy": "icy", "rocky": "rocky"}


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
        self._edsm_fetched: set = set()
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
                self._ensure_edsm(self._system, self._addr)
            elif ev == "FSSDiscoveryScan":
                if e.get("SystemAddress") in (None, self._addr):
                    self._body_count = e.get("BodyCount")
                    self._ensure_edsm(self._system, self._addr)
            elif ev == "Scan":
                self._scan(e)
            elif ev in ("SAASignalsFound", "FSSBodySignals"):
                self._signals(e)
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
        rings_detail = []
        for r in rings:
            rc = _ring_class(r.get("RingClass"))
            short = " ".join(str(r.get("Name") or "").split()[-2:]) or r.get("Name")
            rings_detail.append({"name": short, "ring_class": rc,
                                 "hint": _RING_HINT.get(rc, ""), "mass_mt": r.get("MassMT")})
        # surface materials (raw elements for SRV collection), rarest first
        materials = []
        for m in e.get("Materials") or []:
            nm = str(m.get("Name") or "").lower()
            grade = _RAW_GRADE.get(nm)
            materials.append({"name": nm.capitalize(), "percent": round(float(m.get("Percent") or 0), 1),
                              "grade": grade, "rare": bool(grade and grade >= 3)})
        materials.sort(key=lambda x: (-(x["grade"] or 0), -x["percent"]))
        comp = e.get("Composition") or {}
        composition = ({"ice": round(100 * comp.get("Ice", 0)), "rock": round(100 * comp.get("Rock", 0)),
                        "metal": round(100 * comp.get("Metal", 0))} if comp else None)
        atmos = [{"name": a.get("Name"), "percent": round(float(a.get("Percent") or 0), 1)}
                 for a in (e.get("AtmosphereComposition") or [])]
        prev = self._bodies.get(bid, {})
        self._bodies[bid] = {
            "id": bid, "name": e.get("BodyName", ""), "parent": parent,
            "star": star, "star_type": star_type, "planet_class": planet_class,
            "category": _category(planet_class, star, star_type),
            "kind": (star_type + " star") if star else planet_class,
            "distance_ls": e.get("DistanceFromArrivalLS"),
            "value": value, "landable": bool(e.get("Landable", False)),
            "terraformable": terraform, "terraform_state": e.get("TerraformState") or "",
            "discovered": discovered,
            "mapped": mapped or prev.get("mapped_by_me", False),
            "mapped_by_me": prev.get("mapped_by_me", False),
            "bio_signals": prev.get("bio_signals", 0), "geo_signals": prev.get("geo_signals", 0),
            "rings": len(rings), "rings_detail": rings_detail,
            "reserve_level": _clean(e.get("ReserveLevel")) if rings else "",
            "atmosphere": e.get("Atmosphere") or "", "atmosphere_composition": atmos,
            "volcanism": e.get("Volcanism") or "",
            "materials": materials, "has_rare_mat": any(m["rare"] for m in materials),
            "composition": composition,
            "gravity": e.get("SurfaceGravity"), "temperature": e.get("SurfaceTemperature"),
            "surface_pressure": e.get("SurfacePressure"),
            "mass": mass, "radius": e.get("Radius"),
            "personally_scanned": True,
        }

    # --- EDSM populate-on-honk (so bodies appear like EDD, before you FSS) --
    def _ensure_edsm(self, system: str | None, addr: Any) -> None:
        if not system or addr in self._edsm_fetched:
            return
        self._edsm_fetched.add(addr)
        threading.Thread(target=self._fetch_edsm, args=(system, addr),
                         name="eliteops-system-edsm", daemon=True).start()

    def _fetch_edsm(self, system: str, addr: Any) -> None:
        try:
            data = edsm_client.system_bodies(system)
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            return
        with self._lock:
            if addr != self._addr:
                return  # jumped away while fetching
            if data.get("bodyCount") and not self._body_count:
                self._body_count = data.get("bodyCount")
            for b in data.get("bodies") or []:
                bid = b.get("bodyId")
                if bid is None:
                    continue
                existing = self._bodies.get(bid)
                if existing and existing.get("personally_scanned"):
                    continue  # never clobber a body you actually scanned
                body = self._edsm_to_body(b)
                if body:
                    self._bodies[bid] = body

    def _edsm_to_body(self, b: dict) -> dict | None:
        sub = b.get("subType") or ""
        star = str(b.get("type")) == "Star"
        star_type = _edsm_star_type(sub) if star else ""
        planet_class = "" if star else _edsm_planet_class(sub)
        mass = b.get("solarMasses") if star else b.get("earthMasses")
        terraform = str(b.get("terraformingState") or "") in ("Candidate for terraforming", "Terraformable")
        value = None
        try:
            if mass is not None:
                value = (firsts.star_scan_value(star_type, mass, False) if star
                         else firsts.planet_claim_value(planet_class, terraform, mass, False, False))
        except Exception:  # noqa: BLE001
            value = None
        parent = None
        for p in b.get("parents") or []:
            for kind, pid in p.items():
                if kind in ("Star", "Planet"):
                    parent = pid
                    break
            if parent is not None:
                break
        materials = []
        for nm, pct in (b.get("materials") or {}).items():
            key = str(nm).lower()
            grade = _RAW_GRADE.get(key)
            materials.append({"name": nm, "percent": round(float(pct or 0), 1),
                              "grade": grade, "rare": bool(grade and grade >= 3)})
        materials.sort(key=lambda x: (-(x["grade"] or 0), -x["percent"]))
        sc = b.get("solidComposition") or {}
        composition = ({"ice": round(sc.get("Ice", 0)), "rock": round(sc.get("Rock", 0)),
                        "metal": round(sc.get("Metal", 0))} if sc else None)
        atmos = [{"name": k, "percent": round(float(v or 0), 1)}
                 for k, v in (b.get("atmosphereComposition") or {}).items()]
        rings_detail, ring_ct = [], 0
        for r in b.get("rings") or []:
            if "Belt" in str(r.get("type", "")) or "Belt" in str(r.get("name", "")):
                continue
            ring_ct += 1
            rc = _EDSM_RING.get(str(r.get("type") or "").lower(), "")
            rings_detail.append({"name": " ".join(str(r.get("name") or "").split()[-2:]),
                                 "ring_class": rc, "hint": _RING_HINT.get(rc, ""),
                                 "mass_mt": r.get("mass")})
        atm_type = b.get("atmosphereType") or ""
        volc = b.get("volcanismType") or ""
        return {
            "id": b.get("bodyId"), "name": b.get("name", ""), "parent": parent,
            "star": star, "star_type": star_type, "planet_class": planet_class,
            "category": _category(planet_class, star, star_type),
            "kind": sub or ("star" if star else "body"),
            "distance_ls": b.get("distanceToArrival"), "value": value,
            "landable": bool(b.get("isLandable")), "terraformable": terraform,
            "terraform_state": b.get("terraformingState") or "",
            "discovered": True, "mapped": False, "mapped_by_me": False,
            "bio_signals": 0, "geo_signals": 0,
            "rings": ring_ct, "rings_detail": rings_detail, "reserve_level": "",
            "atmosphere": "" if atm_type in ("", "No atmosphere") else atm_type,
            "atmosphere_composition": atmos,
            "volcanism": "" if volc in ("", "No volcanism") else volc,
            "materials": materials, "has_rare_mat": any(m["rare"] for m in materials),
            "composition": composition,
            "gravity": b.get("gravity"), "temperature": b.get("surfaceTemperature"),
            "surface_pressure": (b.get("surfacePressure") or 0) * 101325 if b.get("surfacePressure") else None,
            "mass": mass, "radius": (b.get("radius") or 0) * 1000 if b.get("radius") else None,
            "personally_scanned": False,
        }

    def _signals(self, e: dict) -> None:
        bid = e.get("BodyID")
        bio = geo = 0
        for s in e.get("Signals") or []:
            t = str(s.get("Type", "")).lower()
            if "biolog" in t:
                bio = s.get("Count", bio)
            elif "geolog" in t:
                geo = s.get("Count", geo)
        b = self._bodies.get(bid)
        if b:
            if bio:
                b["bio_signals"] = bio
            if geo:
                b["geo_signals"] = geo
        elif bid is not None:
            self._bodies[bid] = {"id": bid, "name": e.get("BodyName", ""), "parent": None,
                                 "bio_signals": bio, "geo_signals": geo,
                                 "category": "other", "pending": True}

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
            scanned_by_me = sum(1 for b in bodies.values() if b.get("personally_scanned"))
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
                "scanned": scanned, "scanned_by_me": scanned_by_me,
                "total_value": total_value,
                "undiscovered": undiscovered, "highlights": highlights, "bodies": ordered,
            }
