"""Engineering Materials reference for EliteOps.

A guide tab: how to farm each material category (Raw / Manufactured / Encoded),
the famous farming sites, how Material Traders work, and a live finder for the
nearest Raw / Manufactured / Encoded traders (classified by station economy).
Plus your live material counts. Standard library.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

from . import catalog

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import spansh_client  # noqa: E402

# EDCD rule: a station's material-trader TYPE follows its economy (primary, then
# secondary overrides). Normalise "High Tech" -> "hightech" etc.
_ECON_RULE = {"hightech": "encoded", "military": "encoded",
              "extraction": "raw", "refinery": "raw", "industrial": "manufactured"}


def _mat_trader_type(primary: str, secondary: str) -> str | None:
    kind = None
    for econ in (str(primary or ""), str(secondary or "")):
        e = econ.lower().replace(" ", "")
        if e in _ECON_RULE:
            kind = _ECON_RULE[e]  # secondary (checked last) overrides primary, per the rule
    return kind

TRADER_INFO = (
    "Material Traders swap materials you have for ones you need. Within a category you can "
    "trade DOWN a grade cheaply (3:1) or UP a grade (6:1). Cross-category trades are heavily "
    "taxed — grind the right category instead. Trader type depends on the station economy: "
    "Raw = Extraction/Refinery, Manufactured = Industrial, Encoded = High-Tech/Military."
)

GUIDE = {
    "raw": {
        "label": "Raw Materials",
        "color": "orange",
        "desc": "Elements (iron, carbon … up to rare G4 like Antimony). Surfaces, asteroids, shards.",
        "methods": [
            {"name": "Crystalline Shards", "detail":
             "Best source of the rare G4 raws (Antimony, Polonium, Ruthenium, Technetium, Tellurium, "
             "Yttrium). Land near shard clusters on outer bodies in systems with 2+ widely-spaced "
             "stars; drive up in the SRV and collect. No relog needed — huge stacks fast."},
            {"name": "Surface prospecting", "detail":
             "Shoot mineral/metallic outcrops with the SRV wave scanner on any landable body for "
             "common and mid-grade raws. Relog or move on when depleted."},
            {"name": "Mining", "detail":
             "Asteroid mining drops raw fragments; a quick side-source while you mine for profit."},
        ],
        "sites": [
            {"system": "HIP 36601", "body": "C 1 d / C 3 b", "note": "Legendary crystalline-shard site — Antimony, Tellurium, Ruthenium and more."},
            {"system": "Outotz LS-K d8-3", "body": "B 5 a", "note": "Popular shard site for rare raws."},
        ],
    },
    "manufactured": {
        "label": "Manufactured Materials",
        "color": "gold",
        "desc": "Alloys, capacitors, composites… From combat salvage and signal sources.",
        "methods": [
            {"name": "Dav's Hope", "detail":
             "THE manufactured-materials loop. Land at the abandoned base, drive the marked SRV "
             "circuit collecting material canisters, then relog (menu → exit to main → resume) to "
             "respawn them. Covers most low/mid-grade manufactured."},
            {"name": "High Grade Emissions (HGE)", "detail":
             "Best source of top-tier G5 manufactured (Military Grade Alloys, Proto Radiolic/Light "
             "Alloys, Pharmaceutical Isolators, Core Dynamics Composites). Supercruise inhabited "
             "systems and drop at 'High Grade Emissions' USSs — which G5 drops depends on the "
             "system's economy + state (e.g. Boom/War/Outbreak)."},
            {"name": "Combat salvage", "detail":
             "Blow up ships in Resource Extraction Sites or Combat Zones and scoop the fragments."},
        ],
        "sites": [
            {"system": "Hyades Sector DR-V c2-23", "body": "planet A 5", "note": "Dav's Hope — the classic manufactured-mat farm."},
        ],
    },
    "encoded": {
        "label": "Encoded Data",
        "color": "cyan",
        "desc": "Scan data — emissions, wakes, shield/data patterns. From scanning ships & sites.",
        "methods": [
            {"name": "Jameson Crash Site", "detail":
             "Crashed Cobra Mk III. Scan the wreck and data points with the SRV to quickly stack "
             "many encoded types (including high-grade). Relog to refresh the site."},
            {"name": "Wake scanning", "detail":
             "Fit a Frame Shift Wake Scanner and scan high-wakes at nav beacons / busy systems for "
             "Wake data (e.g. Datamined Wake Exceptions for FSD engineering)."},
            {"name": "Ship scanning", "detail":
             "Passively scan NPC ships (and use Kill Warrant / Manifest scanners) for encoded "
             "emission and data materials while you fly."},
        ],
        "sites": [
            {"system": "HIP 12099", "body": "planet 1 B", "note": "Jameson crashed Cobra — encoded-data goldmine."},
        ],
    },
}


class EngMatEngine:
    def __init__(self, state) -> None:
        self.state = state
        self._lock = threading.Lock()
        self._materials = {"raw": {}, "manufactured": {}, "encoded": {}}
        self._traders = {"status": "idle", "results": {}, "error": ""}
        # index every recipe ingredient by name -> its category (Raw/Manufactured/
        # Encoded/Commodity/...), so the lookup can say how to get any material.
        self._index: dict[str, dict] = {}
        for r in catalog.get()._recipes:
            for i in r.get("Ingredients", []) or []:
                n = i.get("Name")
                if n and n.lower() not in self._index:
                    self._index[n.lower()] = {"name": n, "category": i.get("Category")}
        # material reference (grade + storage cap) — key matches the journal name
        self._ref: list[dict] = []
        try:
            with open(os.path.join(os.path.dirname(__file__), "data", "materials_ref.json"),
                      encoding="utf-8") as fh:
                self._ref = json.load(fh)
        except (OSError, ValueError):
            self._ref = []
        self._ref_by_key = {m["key"]: m for m in self._ref}
        state.add_journal_listener(self.on_journal)

    # --- full inventory grid (held count vs storage cap, per grade) ---------
    def _inventory(self) -> dict:
        cat_map = {"Raw": "raw", "Manufactured": "manufactured", "Encoded": "encoded"}
        groups: dict[str, list] = {"Raw": [], "Manufactured": [], "Encoded": []}
        summary: dict[str, dict] = {}
        for m in self._ref:
            cat = m["cat"]
            count = int(self._materials.get(cat_map.get(cat, ""), {}).get(m["key"], 0))
            cap = m["cap"] or 100
            pct = count / cap if cap else 0
            status = ("full" if count >= cap else "high" if pct >= 0.8
                      else "empty" if count == 0 else "low")
            groups.setdefault(cat, []).append({
                "key": m["key"], "name": m["name"], "grade": m["grade"], "group": m.get("group"),
                "count": count, "cap": cap, "pct": round(pct, 3), "status": status,
            })
        for cat, items in groups.items():
            items.sort(key=lambda x: (x["grade"], x["name"]))
            summary[cat] = {
                "held": sum(1 for i in items if i["count"] > 0),
                "types": len(items),
                "full": sum(1 for i in items if i["status"] == "full"),
                "total": sum(i["count"] for i in items),
            }
        return {"groups": groups, "summary": summary}

    # --- material lookup ("where do I get X?") -----------------------------
    def lookup(self, query: str) -> list[dict]:
        q = str(query or "").strip().lower()
        if len(q) < 2:
            return []
        hits = [e for name, e in self._index.items() if q in name]
        hits.sort(key=lambda e: (not e["name"].lower().startswith(q), e["name"]))
        out = []
        for e in hits[:8]:
            cat = str(e.get("category") or "")
            key = cat.lower()
            g = GUIDE.get(key)
            commodity = cat == "Commodity"
            if commodity:
                where = "Buy at a station market (industrial/machinery economies stock it). Use the Guardian tab's market finder, or the Cargo tab."
                methods = []
            elif g:
                where = g["desc"]
                methods = [m["name"] for m in g["methods"]]
            else:
                where = "Standard engineering material."
                methods = []
            out.append({"name": e["name"], "category": cat, "commodity": commodity,
                        "where": where, "methods": methods,
                        "sites": (g or {}).get("sites", []) if not commodity else []})
        return out

    def on_journal(self, e: dict) -> None:
        if e.get("event") != "Materials":
            return
        with self._lock:
            for cat in ("Raw", "Manufactured", "Encoded"):
                bucket = {}
                for it in e.get(cat, []) or []:
                    if it.get("Name"):
                        bucket[it["Name"].lower()] = int(it.get("Count", 0))
                self._materials[cat.lower()] = bucket

    # --- trader finder (Spansh, on demand) ---------------------------------
    def find_traders(self) -> None:
        with self._lock:
            self._traders = {"status": "searching", "results": {}, "error": ""}
        threading.Thread(target=self._run_traders, name="eliteops-traders", daemon=True).start()

    def _run_traders(self) -> None:
        try:
            ref = self.state.snapshot().get("system")
            if not ref:
                raise ValueError("No reference system — jump in-game first.")
            body = {"filters": {"distance": {"min": "0", "max": "150"},
                                "services": {"value": ["Material Trader"]},
                                "type": {"value": spansh_client._ACQUIRE_STATION_TYPES}},
                    "sort": [{"distance": {"direction": "asc"}}],
                    "reference_system": ref, "size": 30}
            data = spansh_client._jpost("/stations/search", body, timeout=30)
            groups: dict[str, list] = {"raw": [], "manufactured": [], "encoded": [], "other": []}
            for s in (data.get("results") if isinstance(data, dict) else []) or []:
                if "Carrier" in str(s.get("type") or ""):
                    continue
                econ = s.get("primary_economy") or s.get("economy") or ""
                kind = _mat_trader_type(s.get("primary_economy"), s.get("secondary_economy")) or "other"
                row = {"system": s.get("system_name"), "station": s.get("name"),
                       "distance_ly": s.get("distance"), "distance_ls": s.get("distance_to_arrival"),
                       "economy": econ, "large_pad": s.get("has_large_pad")}
                if len(groups[kind]) < 3:
                    groups[kind].append(row)
            with self._lock:
                self._traders = {"status": "ready", "results": groups, "error": "", "reference": ref}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._traders = {"status": "error", "results": {}, "error": str(exc)}

    # --- snapshot -----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            counts = {c: {"types": len(m), "total": sum(m.values())}
                      for c, m in self._materials.items()}
            return {
                "materials": counts,
                "inventory": self._inventory(),
                "guide": GUIDE,
                "trader_info": TRADER_INFO,
                "traders": self._traders,
            }
