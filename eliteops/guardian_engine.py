"""Guardian module unlock planner for EliteOps.

Pick the Guardian modules you want -> see the exact Guardian materials each needs to
unlock at a Guardian Technology Broker (from the bundled recipe catalog), diffed
against your live inventory, plus the nearest Tech Broker (Spansh) and a farming
guide (which materials come from Guardian Ruins vs Structures). Standard library.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

from . import catalog

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import spansh_client  # noqa: E402

# Curated Guardian modules -> the recipe that unlocks the line at the Tech Broker.
# (Unlocking the base recipe lets you buy the rest of the sizes at outfitting.)
GUARDIAN_MODULES = [
    {"key": "gfsb", "name": "Guardian FSD Booster", "group": "Utility", "recipe": "Guardian FSD Booster Size 1"},
    {"key": "ghrp", "name": "Guardian Hull Reinforcement", "group": "Defence", "recipe": "Guardian Hull Reinforcement Size 1 Class 1"},
    {"key": "gmrp", "name": "Guardian Module Reinforcement", "group": "Defence", "recipe": "Guardian Module Reinforcement Size 1 Class 1"},
    {"key": "gsrp", "name": "Guardian Shield Reinforcement", "group": "Defence", "recipe": "Guardian Shield Reinforcement Size 1 Class 1"},
    {"key": "gpd", "name": "Guardian Hybrid Power Distributor", "group": "Core", "recipe": "Guardian Power Distributor Size 1"},
    {"key": "gpp", "name": "Guardian Hybrid Power Plant", "group": "Core", "recipe": "Guardian Power Plant Size 2"},
    {"key": "ggc", "name": "Guardian Gauss Cannon", "group": "Weapon", "recipe": "Guardian Gauss Cannon Fixed Small"},
    {"key": "gsc", "name": "Guardian Shard Cannon", "group": "Weapon", "recipe": "Guardian Shard Cannon Fixed Small"},
    {"key": "gpc", "name": "Guardian Plasma Charger", "group": "Weapon", "recipe": "Guardian Plasma Launcher Fixed Small"},
]

# Which Guardian material comes from where (by keyword in the display name).
_STRUCTURE_KEYS = ("power cell", "power conduit", "technology component", "wreckage",
                   "sentinel", "weapon parts")
_RUINS_KEYS = ("obelisk", "blueprint fragment", "pattern")


def _classify(name: str, category: str) -> str:
    """Where/how to get an unlock ingredient: Ruins / Structures (Guardian sites),
    Buy (a market commodity), or Farm (a standard engineering material)."""
    low = str(name or "").lower()
    if any(k in low for k in _RUINS_KEYS):
        return "Ruins"
    if any(k in low for k in _STRUCTURE_KEYS):
        return "Structures"
    if str(category or "") == "Commodity":
        return "Buy"
    return "Farm"


# one-line "how do I get this" per source (Farm keyed by material category)
_HOW_SOURCE = {
    "Ruins": "Scan the obelisks at a Guardian Ruins site with your SRV.",
    "Structures": "Destroy the Sentinels at a Guardian Structure with your SRV.",
    "Buy": "Buy it at a station market — hit ⌘ market for the nearest source.",
}
_HOW_FARM = {
    "Manufactured": "Manufactured material — Dav's Hope loop, combat salvage, or a Manufactured Material Trader.",
    "Raw": "Raw material — Crystalline Shards, surface prospecting, or a Raw Material Trader.",
    "Encoded": "Encoded data — Jameson Crash Site, wake/ship scanning, or an Encoded Material Trader.",
}


def _how(source: str, category: str) -> str:
    if source == "Farm":
        return _HOW_FARM.get(str(category or ""), "Standard engineering material.")
    return _HOW_SOURCE.get(source, "")


# curated, community-stable farming hotspots (system is what matters for travel)
GUARDIAN_SITES = [
    {"system": "Synuefe EU-Q c21-10", "type": "Structure",
     "note": "Classic Sentinel-components farm (body 5) — power cells/conduits, tech & wreckage components, weapon parts."},
    {"system": "Synuefe XR-H d11-102", "type": "Ruins",
     "note": "Guardian Ruins — scan obelisks for Pattern data and blueprint fragments."},
    {"system": "Synuefe LY-I b42-2", "type": "Structure",
     "note": "Guardian Structure — alternate Sentinel-components site."},
]


class GuardianEngine:
    def __init__(self, state) -> None:
        self.state = state
        self.cat = catalog.get()
        self._lock = threading.Lock()
        self._materials: dict[str, int] = {}      # fdname(lower) -> count
        self._wanted: set[str] = set()            # selected module keys
        self._broker: dict = {"status": "idle", "stops": [], "error": ""}
        self._markets: dict[str, dict] = {}       # commodity name -> {status, stops, error}
        # index recipes by name
        self._recipe_by_name = {r.get("Name"): r for r in self.cat._recipes if r.get("Grade") == -1}
        state.add_journal_listener(self.on_journal)

    # --- live materials -----------------------------------------------------
    def on_journal(self, e: dict) -> None:
        ev = e.get("event")
        with self._lock:
            if ev == "Materials":
                self._materials = {}
                for cat_key in ("Raw", "Manufactured", "Encoded"):
                    for it in e.get(cat_key, []) or []:
                        if it.get("Name"):
                            self._materials[it["Name"].lower()] = int(it.get("Count", 0))
            elif ev == "MaterialCollected":
                n = str(e.get("Name") or "").lower()
                if n:
                    self._materials[n] = self._materials.get(n, 0) + int(e.get("Count", 0))
            elif ev == "MaterialDiscarded":
                n = str(e.get("Name") or "").lower()
                if n:
                    self._materials[n] = max(0, self._materials.get(n, 0) - int(e.get("Count", 0)))

    def _owned(self, fd: str) -> int:
        return self._materials.get(str(fd or "").lower(), 0)

    # --- selection ----------------------------------------------------------
    def set_wanted(self, keys: list[str]) -> None:
        valid = {m["key"] for m in GUARDIAN_MODULES}
        with self._lock:
            self._wanted = {k for k in (keys or []) if k in valid}

    def toggle(self, key: str) -> None:
        with self._lock:
            if key in self._wanted:
                self._wanted.discard(key)
            elif any(m["key"] == key for m in GUARDIAN_MODULES):
                self._wanted.add(key)

    def persist(self) -> dict:
        with self._lock:
            return {"wanted": list(self._wanted)}

    def restore(self, data: dict | None) -> None:
        if data:
            self.set_wanted(data.get("wanted") or [])

    # --- tech broker (Spansh, on demand) -----------------------------------
    def find_broker(self) -> None:
        with self._lock:
            self._broker = {"status": "searching", "stops": [], "error": ""}
        threading.Thread(target=self._run_broker, name="eliteops-guardian-broker", daemon=True).start()

    def _run_broker(self) -> None:
        # PRIMARY: Inara (real service data + Guardian/Human broker type). On its
        # bot-check block or any error, fall back to Spansh High-Tech-economy
        # candidates (a Guardian broker sits at High Tech) and hand the user a
        # browser deep-link to Inara's confirmed list.
        from . import inara_client
        ref = self.state.snapshot().get("system")
        if not ref:
            with self._lock:
                self._broker = {"status": "error", "stops": [],
                                "error": "No reference system — jump in-game first."}
            return
        inara_url = inara_client.nearest_url(ref, "broker_guardian")
        try:
            stops = [{"system": s.get("system"), "station": s.get("station"),
                      "distance_ly": s.get("distance_ly"), "distance_ls": s.get("station_dist_ls"),
                      "economy": s.get("economy"), "allegiance": s.get("allegiance")}
                     for s in inara_client.nearest_stations(ref, "broker_guardian", limit=8)]
            with self._lock:
                self._broker = {"status": "ready", "stops": stops, "error": "",
                                "reference": ref, "source": "inara", "inara_url": inara_url}
            return
        except inara_client.InaraBlocked:
            note = ("Inara is blocking automated lookups right now (its bot-check). Showing "
                    "EDSM's confirmed Guardian tech brokers instead. Use “Open in Inara” for "
                    "the full list.")
        except Exception as exc:  # noqa: BLE001
            note = f"Inara lookup unavailable ({exc}). Showing EDSM's confirmed Guardian brokers."

        # FALLBACK 1: EDSM — real per-station service data (Technology Broker + High Tech).
        try:
            from . import edsm_traders
            stops = edsm_traders.nearest_guardian_brokers(ref)
            if stops:
                with self._lock:
                    self._broker = {"status": "ready", "stops": stops, "error": "", "reference": ref,
                                    "source": "edsm", "note": note, "inara_url": inara_url}
                return
        except Exception:  # noqa: BLE001 — EDSM down too; drop to economy guess
            pass

        # FALLBACK 2: Spansh High-Tech economy candidates (presence NOT guaranteed).
        try:
            stops = self._spansh_broker(ref)
            with self._lock:
                self._broker = {"status": "ready", "stops": stops, "error": "", "reference": ref,
                                "source": "spansh-fallback",
                                "note": note + " (EDSM had nothing nearby — these are High-Tech "
                                               "Spansh guesses; verify on arrival.)",
                                "inara_url": inara_url}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._broker = {"status": "error", "stops": [], "error": str(exc),
                                "note": note, "inara_url": inara_url}

    def _spansh_broker(self, ref: str) -> list[dict]:
        """Fallback: nearest High-Tech stations (Guardian brokers live at High Tech).
        Presence not guaranteed — verify or use the Inara link."""
        seen, stops = set(), []
        for econ_key in ("primary_economy", "secondary_economy"):
            body = {"filters": {"distance": {"min": "0", "max": "500"},
                                "type": {"value": spansh_client._ACQUIRE_STATION_TYPES},
                                econ_key: {"value": ["High Tech"]}},
                    "sort": [{"distance": {"direction": "asc"}}],
                    "reference_system": ref, "size": 30}
            data = spansh_client._jpost("/stations/search", body, timeout=30)
            for s in (data.get("results") if isinstance(data, dict) else []) or []:
                name = str(s.get("name") or "")
                if "Carrier" in str(s.get("type") or "") or name.startswith("$"):
                    continue
                k = (s.get("system_name"), name)
                if k in seen:
                    continue
                seen.add(k)
                stops.append({"system": s.get("system_name"), "station": name,
                              "distance_ly": s.get("distance"), "distance_ls": s.get("distance_to_arrival"),
                              "economy": s.get("primary_economy") or s.get("economy") or "",
                              "large_pad": s.get("has_large_pad")})
        stops.sort(key=lambda x: x["distance_ly"] if x["distance_ly"] is not None else 1e9)
        return stops[:8]

    # --- commodity market finder (for 'Buy' ingredients) -------------------
    def find_market(self, commodity: str) -> None:
        commodity = str(commodity or "").strip()
        if not commodity:
            return
        with self._lock:
            self._markets[commodity] = {"status": "searching", "stops": [], "error": ""}
        threading.Thread(target=self._run_market, args=(commodity,),
                         name="eliteops-guardian-market", daemon=True).start()

    def find_all_markets(self) -> None:
        """Search every 'Buy' commodity needed by the selected unlocks at once
        (in parallel), so it's one click instead of chasing each commodity."""
        with self._lock:
            wanted = set(self._wanted)
        names: list[str] = []
        for m in GUARDIAN_MODULES:
            if m["key"] not in wanted:
                continue
            recipe = self._recipe_by_name.get(m["recipe"])
            for i in (recipe or {}).get("Ingredients", []) or []:
                if str(i.get("Category") or "") == "Commodity":
                    nm = i.get("Name")
                    if nm and nm not in names:
                        names.append(nm)
        with self._lock:
            for nm in names:
                self._markets[nm] = {"status": "searching", "stops": [], "error": ""}
        for nm in names:
            threading.Thread(target=self._run_market, args=(nm,),
                             name="eliteops-guardian-market", daemon=True).start()

    def _run_market(self, commodity: str) -> None:
        ref = self.state.snapshot().get("system")
        if not ref:
            with self._lock:
                self._markets[commodity] = {"status": "error", "stops": [],
                                            "error": "No reference system — jump in-game first."}
            return
        err = None
        for attempt in range(3):  # Spansh market search is slow and can 502/timeout under load
            try:
                sources = spansh_client.find_commodity_sources(ref, commodity, 1,
                                                               large_pad_only=False, limit=5)
                stops = [{"system": s.get("system"), "station": s.get("station"),
                          "distance_ly": s.get("distance_ly"), "distance_ls": s.get("distance_ls"),
                          "buy_price": s.get("buy_price"), "supply": s.get("supply")}
                         for s in sources]
                with self._lock:
                    self._markets[commodity] = {"status": "ready", "stops": stops, "error": "", "reference": ref}
                return
            except Exception as exc:  # noqa: BLE001
                err = exc
                time.sleep(2 * (attempt + 1))
        with self._lock:
            self._markets[commodity] = {"status": "error", "stops": [], "error": str(err)}

    # --- snapshot -----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            modules = []
            for m in GUARDIAN_MODULES:
                recipe = self._recipe_by_name.get(m["recipe"])
                ings = (recipe or {}).get("Ingredients", []) or []
                ready = all(self._owned(i.get("FdName")) >= int(i.get("Amount", 0)) for i in ings)
                modules.append({**{k: m[k] for k in ("key", "name", "group")},
                                "wanted": m["key"] in self._wanted,
                                "ingredient_count": len(ings), "ready": ready and bool(ings)})

            # aggregate requirements across wanted modules
            need: dict[str, dict] = {}
            for m in GUARDIAN_MODULES:
                if m["key"] not in self._wanted:
                    continue
                recipe = self._recipe_by_name.get(m["recipe"])
                for i in (recipe or {}).get("Ingredients", []) or []:
                    fd = (i.get("FdName") or "").lower()
                    if not fd:
                        continue
                    slot = need.setdefault(fd, {"fd": fd, "name": i.get("Name"),
                                                "category": i.get("Category"),
                                                "source": _classify(i.get("Name"), i.get("Category")),
                                                "commodity": str(i.get("Category") or "") == "Commodity",
                                                "needed": 0, "usedBy": set()})
                    slot["needed"] += int(i.get("Amount", 0))
                    slot["usedBy"].add(m["name"])

            materials, total_short = [], 0
            for slot in need.values():
                owned = self._owned(slot["fd"])
                short = max(0, slot["needed"] - owned)
                total_short += short
                materials.append({"name": slot["name"], "category": slot["category"],
                                  "source": slot["source"], "commodity": slot["commodity"],
                                  "how": _how(slot["source"], slot["category"]),
                                  "needed": slot["needed"], "owned": owned, "short": short,
                                  "usedBy": sorted(slot["usedBy"])})
            materials.sort(key=lambda x: (-x["short"], x["source"], x["name"] or ""))

            by_source = {}
            for mt in materials:
                if mt["short"] > 0:
                    by_source.setdefault(mt["source"], 0)
                    by_source[mt["source"]] += mt["short"]

            return {
                "modules": modules,
                "materials": materials,
                "total_short": total_short,
                "by_source": by_source,
                "sites": GUARDIAN_SITES,
                "broker": self._broker,
                "markets": self._markets,
                "wanted_count": len(self._wanted),
            }
