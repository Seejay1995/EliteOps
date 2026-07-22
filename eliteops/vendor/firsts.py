"""Firsts detection engine for the RouteOps Firsts Radar.

Reads the Elite Dangerous journal and tracks, for the CURRENT system, which
bodies still offer a "first": first discovery, first map, or first footfall --
plus a running session tally of firsts achieved. Standard library only, no
EDDiscovery dependency (tails the journal files directly).

The three per-body flags all come from the `Scan` event:
  WasDiscovered / WasMapped / WasFootfalled  (False == nobody has, it's available)
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_JOURNAL_DIR = os.path.join(
    os.path.expanduser("~"), "Saved Games", "Frontier Developments", "Elite Dangerous"
)

_Q = 0.56591828  # exploration value mass exponent constant


def _starclass_k(star_type: str) -> int:
    if star_type in ("N", "H"):   # neutron star / black hole
        return 22628
    if star_type.startswith("D"):  # white dwarf
        return 14057
    return 1200


def _planetclass_k(planet_class: str, terraformable: bool) -> tuple[int, int, float]:
    """(base, terraform_bonus, min_multiplier). MattG's formula via EDMC-Pioneer."""
    terraform, mult = 0, 1.0
    if planet_class == "Metal rich body":
        base = 21790
    elif planet_class == "Ammonia world":
        base = 96932
    elif planet_class == "Sudarsky class I gas giant":
        base = 1656
    elif planet_class in ("Sudarsky class II gas giant", "High metal content body"):
        base = 9654
        if terraformable:
            terraform, mult = 100677, 0.9
    elif planet_class == "Water world":
        base = 64831
        if terraformable:
            terraform, mult = 116295, 0.75
    elif planet_class == "Earthlike body":
        base, terraform = 64831, 116295
        mult = 1.0 if not terraformable else 0.0
    else:
        base = 300
        if terraformable:
            terraform, mult = 93328, 0.9
    return base, terraform, mult


def star_scan_value(star_type: str, mass: float, first_discoverer: bool) -> int:
    k = _starclass_k(star_type)
    value = k + (mass * k / 66.25)
    if first_discoverer:
        value *= 2.6
    return round(value)


def planet_claim_value(planet_class: str, terraformable: bool, mass: float,
                       first_discoverer: bool, first_mapper: bool) -> int:
    """Full value earned scanning + mapping this planet, with the given first bonuses."""
    base, terraform, _ = _planetclass_k(planet_class, terraformable)
    k = base + terraform
    if first_discoverer and first_mapper:
        mapping = 3.699622554
    elif first_mapper:
        mapping = 8.0956
    else:
        mapping = 10 / 3
    value = k + k * _Q * (mass ** 0.2)
    mapped = value * mapping
    mapped += (mapped * 0.3) if (mapped * 0.3) > 555 else 555  # Odyssey/4.0 bonus
    mapped = max(mapped, 500)
    if first_discoverer:
        mapped *= 2.6
    return round(mapped)


def journal_files(journal_dir: str | None = None) -> list[str]:
    journal_dir = journal_dir or DEFAULT_JOURNAL_DIR
    files = glob.glob(os.path.join(journal_dir, "Journal.*.log"))
    return sorted(files, key=os.path.getmtime)


def latest_journal(journal_dir: str | None = None) -> str | None:
    files = journal_files(journal_dir)
    return files[-1] if files else None


@dataclass
class Body:
    body_id: int
    name: str = ""
    kind: str = ""            # display type
    star: bool = False        # True if scanned as a star (has StarType, not PlanetClass)
    planet_class: str = ""    # raw PlanetClass
    star_type: str = ""       # raw StarType letter
    mass: float | None = None  # MassEM (planets) or StellarMass (stars)
    terraformable: bool = False
    landable: bool = False
    distance_ls: float | None = None
    bio_signals: int = 0
    # first-flags as reported by the game (False == available to claim)
    discovered: bool = True
    mapped: bool = True
    footfalled: bool = True
    mapped_by_me: bool = False
    footfalled_by_me: bool = False

    @property
    def is_planet(self) -> bool:
        # DSS-mappable bodies are planets/moons (have a PlanetClass), never stars.
        return bool(self.planet_class)

    def value(self) -> int | None:
        """Estimated credits earned by claiming the firsts available on this body."""
        if self.mass is None:
            return None
        if self.star:
            return star_scan_value(self.star_type, self.mass, self.first_discovery)
        if self.planet_class:
            return planet_claim_value(
                self.planet_class, self.terraformable, self.mass,
                self.first_discovery, self.first_map,
            )
        return None

    @property
    def first_discovery(self) -> bool:
        return not self.discovered

    @property
    def first_map(self) -> bool:
        # DSS-mappable surface (planets/moons), not yet mapped
        return self.is_planet and not self.mapped

    @property
    def first_footfall(self) -> bool:
        return self.landable and not self.footfalled

    @property
    def has_first(self) -> bool:
        return self.first_discovery or self.first_map or self.first_footfall

    def badges(self) -> list[str]:
        out: list[str] = []
        if self.first_discovery:
            out.append("DISCOVERY")
        if self.first_map:
            out.append("MAP")
        if self.first_footfall:
            out.append("FOOTFALL")
        if self.bio_signals and not self.discovered:
            out.append("BIO~FIRST")  # bio in an undiscovered system -> almost certainly first-logged
        return out


@dataclass
class Radar:
    """Rolling journal state. Feed it events with .apply()."""

    system: str = ""
    system_address: int | None = None
    body_count: int | None = None
    bodies: dict[int, Body] = field(default_factory=dict)
    tally: dict[str, int] = field(default_factory=lambda: {
        "first_discovery": 0, "first_map": 0, "first_footfall": 0,
        "codex_new": 0, "first_logged_sold": 0,
    })
    _seen_discovery: set[str] = field(default_factory=set)
    _seen_map: set[str] = field(default_factory=set)
    _seen_footfall: set[str] = field(default_factory=set)

    def _key(self, body_id: Any) -> str:
        return f"{self.system_address}:{body_id}"

    def _enter_system(self, name: Any, address: Any) -> None:
        if address is not None and address == self.system_address:
            return  # same system (e.g. relog / Location after a jump)
        self.system = str(name or "")
        self.system_address = address
        self.body_count = None
        self.bodies = {}

    def apply(self, event: dict[str, Any]) -> None:
        ev = event.get("event")
        if ev in ("FSDJump", "CarrierJump", "Location"):
            self._enter_system(event.get("StarSystem"), event.get("SystemAddress"))
        elif ev == "FSSDiscoveryScan":
            if event.get("SystemAddress") == self.system_address:
                self.body_count = event.get("BodyCount")
        elif ev == "Scan":
            self._scan(event)
        elif ev == "SAASignalsFound":
            self._bio_signals(event)
        elif ev == "SAAScanComplete":
            self._mapped_by_me(event)
        elif ev in ("Touchdown", "Disembark"):
            self._footfall_by_me(event)
        elif ev == "CodexEntry" and event.get("IsNewEntry"):
            self.tally["codex_new"] += 1
        elif ev == "SellOrganicData":
            self.tally["first_logged_sold"] += sum(
                1 for bio in event.get("BioData", []) if (bio.get("Bonus") or 0) > 0
            )

    def _scan(self, e: dict[str, Any]) -> None:
        body_id = e.get("BodyID")
        if body_id is None:
            return
        if e.get("SystemAddress") not in (None, self.system_address):
            # scan belongs to a different system than we think we're in
            self._enter_system(e.get("StarSystem"), e.get("SystemAddress"))
        body = self.bodies.get(body_id) or Body(body_id=body_id)
        body.name = e.get("BodyName", body.name)
        if e.get("PlanetClass"):
            body.planet_class = e["PlanetClass"]
            body.kind, body.star = e["PlanetClass"], False
            body.terraformable = str(e.get("TerraformState") or "") in ("Terraformable", "Terraforming")
            if e.get("MassEM") is not None:
                body.mass = e.get("MassEM")
        elif e.get("StarType"):
            body.star_type = e["StarType"]
            body.kind, body.star = e["StarType"] + " star", True
            if e.get("StellarMass") is not None:
                body.mass = e.get("StellarMass")
        body.landable = bool(e.get("Landable", body.landable))
        if e.get("DistanceFromArrivalLS") is not None:
            body.distance_ls = e.get("DistanceFromArrivalLS")
        for attr, keyname in (("discovered", "WasDiscovered"), ("mapped", "WasMapped"), ("footfalled", "WasFootfalled")):
            if keyname in e:
                setattr(body, attr, bool(e[keyname]))
        self.bodies[body_id] = body
        # first DISCOVERY is claimed the instant you scan an undiscovered body
        if body.first_discovery and self._key(body_id) not in self._seen_discovery:
            self._seen_discovery.add(self._key(body_id))
            self.tally["first_discovery"] += 1

    def _bio_signals(self, e: dict[str, Any]) -> None:
        body_id = e.get("BodyID")
        if body_id is None:
            return
        body = self.bodies.get(body_id) or Body(body_id=body_id, name=e.get("BodyName", ""))
        count = 0
        for sig in e.get("Signals", []):
            t = str(sig.get("Type", "")).lower()
            if "biological" in t:
                count = sig.get("Count", count)
        body.bio_signals = count or body.bio_signals
        self.bodies[body_id] = body

    def _mapped_by_me(self, e: dict[str, Any]) -> None:
        body_id = e.get("BodyID")
        body = self.bodies.get(body_id) if body_id is not None else None
        first = bool(body and not body.mapped)
        if body:
            body.mapped_by_me = True
        if first and body_id is not None and self._key(body_id) not in self._seen_map:
            self._seen_map.add(self._key(body_id))
            self.tally["first_map"] += 1

    def _footfall_by_me(self, e: dict[str, Any]) -> None:
        body_id = e.get("BodyID")
        if not e.get("OnPlanet", True) or body_id is None:
            return
        body = self.bodies.get(body_id)
        first = bool(body and not body.footfalled)
        if body:
            body.footfalled_by_me = True
        if first and self._key(body_id) not in self._seen_footfall:
            self._seen_footfall.add(self._key(body_id))
            self.tally["first_footfall"] += 1

    # --- read helpers for the UI ---------------------------------------------
    @property
    def undiscovered_system(self) -> bool:
        """True if every scanned body in the system was undiscovered (a fresh system)."""
        scanned = [b for b in self.bodies.values() if b.name]
        return bool(scanned) and all(not b.discovered for b in scanned)

    def rows(self) -> list[Body]:
        """Current-system bodies, firsts first, then by value, then distance."""
        return sorted(
            self.bodies.values(),
            key=lambda b: (not b.has_first, -(b.value() or 0), b.distance_ls if b.distance_ls is not None else 1e18),
        )

    def firsts_value(self) -> int:
        """Total estimated credits from the firsts still available in this system."""
        return sum(b.value() or 0 for b in self.bodies.values() if b.has_first)


def replay(events: Iterable[dict[str, Any]], radar: Radar | None = None) -> Radar:
    radar = radar or Radar()
    for event in events:
        radar.apply(event)
    return radar


def read_events(path: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return events
