"""Exploration 'data wallet' for EliteOps — what unsold data you're carrying.

The Firsts radar values only the system you're in and forgets it on the next
jump. This engine keeps a session-wide ledger of the universe-cartographic data
you've scanned/mapped across the whole trip and its estimated sell value, so you
can see what's at risk before you rebuy. Cleared (per system) when you actually
sell at Universal Cartographics. Bio value is pulled from the exo engine, which
already tracks it exactly. Standard library only.

Cartographic credits are an ESTIMATE (community value formula); bio is exact.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import firsts  # noqa: E402 — reuse its value formulas


class DataEngine:
    def __init__(self, state, exo) -> None:
        self.state = state
        self.exo = exo
        self._lock = threading.Lock()
        self._bodies: dict[str, dict] = {}    # "sysaddr:bodyid" -> body value record
        self._sysname: dict[Any, str] = {}    # SystemAddress -> name (Scan has no name)
        self._sold_value = 0
        self._sold_bodies = 0
        state.add_journal_listener(self.on_journal)

    # --- journal ------------------------------------------------------------
    def on_journal(self, e: dict) -> None:
        ev = e.get("event")
        with self._lock:
            if ev in ("FSDJump", "CarrierJump", "Location"):
                addr = e.get("SystemAddress")
                if addr is not None and e.get("StarSystem"):
                    self._sysname[addr] = e["StarSystem"]
            elif ev == "Scan":
                self._on_scan(e)
            elif ev == "SAAScanComplete":
                b = self._bodies.get(f"{e.get('SystemAddress')}:{e.get('BodyID')}")
                if b:
                    b["mapped"] = True
            elif ev in ("SellExplorationData", "MultiSellExplorationData"):
                self._on_sell(e)

    def _on_scan(self, e: dict) -> None:
        addr, bid = e.get("SystemAddress"), e.get("BodyID")
        if addr is None or bid is None:
            return
        first_disc = not e.get("WasDiscovered", False)
        system = e.get("StarSystem") or self._sysname.get(addr) or (self.state.snapshot().get("system") or "")
        key = f"{addr}:{bid}"
        star, planet = e.get("StarType"), e.get("PlanetClass")
        if star:
            v = firsts.star_scan_value(star, float(e.get("StellarMass") or 0), first_disc)
            rec = {"kind": "star", "mapped": True, "first_map": False, "v_scan": v, "v_map": v}
        elif planet:
            mass = float(e.get("MassEM") or 0)
            terra = bool(e.get("TerraformState"))
            first_map = not e.get("WasMapped", False)
            rec = {"kind": "planet", "mapped": False, "first_map": first_map,
                   "v_scan": firsts.planet_scan_value(planet, terra, mass, first_disc),
                   "v_map": firsts.planet_claim_value(planet, terra, mass, first_disc, first_map)}
        else:
            return  # belt cluster / barycentre / ring — no cartographic value
        rec.update({"name": e.get("BodyName") or "?", "system": system, "first_disc": first_disc})
        self._bodies[key] = rec

    def _on_sell(self, e: dict) -> None:
        self._sold_value += int(e.get("TotalEarnings") or
                                (int(e.get("BaseValue") or 0) + int(e.get("Bonus") or 0)))
        # figure out which systems were sold, and drop those held bodies
        sold_names = set()
        for s in e.get("Systems") or []:               # SellExplorationData
            sold_names.add(str(s).lower())
        for d in e.get("Discovered") or []:             # MultiSell (dicts) / SellExpl (names)
            name = d.get("SystemName") if isinstance(d, dict) else d
            if name:
                sold_names.add(str(name).lower())
        if not sold_names:                              # unknown scope -> assume sold everything
            self._sold_bodies += len(self._bodies)
            self._bodies.clear()
            return
        for key in [k for k, b in self._bodies.items() if b["system"].lower() in sold_names]:
            del self._bodies[key]
            self._sold_bodies += 1

    def _held(self, b: dict) -> int:
        return b["v_map"] if b["mapped"] else b["v_scan"]

    # --- persistence (survive a server restart) -----------------------------
    def persist(self) -> dict:
        with self._lock:
            return {"bodies": self._bodies, "sysname": {str(k): v for k, v in self._sysname.items()},
                    "sold_value": self._sold_value, "sold_bodies": self._sold_bodies}

    def restore(self, data: dict | None) -> None:
        if not data:
            return
        with self._lock:
            self._bodies = data.get("bodies") or {}
            self._sysname = data.get("sysname") or {}
            self._sold_value = int(data.get("sold_value") or 0)
            self._sold_bodies = int(data.get("sold_bodies") or 0)

    # --- snapshot -----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            bodies = list(self._bodies.values())
            held = sum(self._held(b) for b in bodies)
            top = sorted(bodies, key=lambda b: -self._held(b))[:10]
            carto = {
                "held": held,
                "systems": len({b["system"] for b in bodies if b["system"]}),
                "bodies": len(bodies),
                "first_discoveries": sum(1 for b in bodies if b["first_disc"]),
                "first_maps": sum(1 for b in bodies if b["mapped"] and b["first_map"]),
                "unmapped_planets": sum(1 for b in bodies if b["kind"] == "planet" and not b["mapped"]),
                "sold_value": self._sold_value,
                "sold_bodies": self._sold_bodies,
                "top": [{"name": b["name"], "system": b["system"], "kind": b["kind"],
                         "mapped": b["mapped"], "first_disc": b["first_disc"],
                         "first_map": b["mapped"] and b["first_map"], "value": self._held(b)}
                        for b in top],
            }
        bio = self.exo.held_bio()
        return {"cartographic": carto, "bio": bio,
                "at_risk": carto["held"] + bio["value"]}
