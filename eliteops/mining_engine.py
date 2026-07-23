"""Mining cockpit HUD for EliteOps.

Everything a mining ship's dashboard wants, from the journal + Cargo.json:
  * Limpets remaining (the "drones" commodity) with a low warning.
  * Cargo hold — which ores you're carrying and how much, valuable ones highlighted.
  * Last prospected asteroid — content level, per-material %, core motherlode.
  * This session — tons refined per ore, asteroids prospected, cores cracked.
  * Beginner coaching that reacts to what you're doing (you've never mined before).
Plus a live "where to sell" via Spansh. Standard library only.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import spansh_client  # noqa: E402


def _sq(v: Any) -> str:
    return "".join(c for c in str(v or "").lower() if c.isalnum())


def _strip_sym(v: Any) -> str:
    s = str(v or "")
    if s.startswith("$") and s.endswith(";"):
        s = s[1:-1]
        if s.endswith("_name"):
            s = s[:-5]
    return s


# Bundled minables reference: squashed name -> (display, tier, method, note).
# tier: hot (top earners) | good | fuel | common.  method: laser | core | both.
_MINABLES = {
    # --- laser hotspots ---
    "painite": ("Painite", "hot", "both", "Classic high-value laser target; also a core motherlode."),
    "platinum": ("Platinum", "good", "laser", "Reliable laser earner in metallic rings/icy hotspots."),
    "tritium": ("Tritium", "fuel", "both", "Fleet-carrier fuel — always in demand."),
    "bromellite": ("Bromellite", "good", "both", "Good laser/core value in icy rings."),
    "osmium": ("Osmium", "good", "laser", "Decent metallic laser ore."),
    "palladium": ("Palladium", "good", "laser", "Metallic laser ore."),
    "gold": ("Gold", "common", "laser", "Low-mid value metallic ore."),
    "silver": ("Silver", "common", "laser", "Low-mid value metallic ore."),
    # --- core (crack the asteroid) gems ---
    "voidopal": ("Void Opals", "hot", "core", "Core-mining gem — crack the rock, collect chunks."),
    "voidopals": ("Void Opals", "hot", "core", "Core-mining gem — crack the rock, collect chunks."),
    "opal": ("Void Opals", "hot", "core", "Core-mining gem — crack the rock, collect chunks."),
    "lowtemperaturediamond": ("Low Temperature Diamonds", "hot", "core", "Top core gem in icy rings."),
    "lowtemperaturediamonds": ("Low Temperature Diamonds", "hot", "core", "Top core gem in icy rings."),
    "monazite": ("Monazite", "hot", "core", "High-value core gem in metal-rich rings."),
    "musgravite": ("Musgravite", "good", "core", "Core gem."),
    "alexandrite": ("Alexandrite", "good", "core", "Core gem in icy rings."),
    "grandidierite": ("Grandidierite", "good", "core", "Core gem."),
    "benitoite": ("Benitoite", "good", "core", "Core gem in metal-rich rings."),
    "serendibite": ("Serendibite", "good", "core", "Core gem."),
    "rhodplumsite": ("Rhodplumite", "good", "core", "Core gem in metal-rich rings."),
    "rhodplumite": ("Rhodplumite", "good", "core", "Core gem in metal-rich rings."),
    # --- common / low value ---
    "bertrandite": ("Bertrandite", "common", "laser", "Low value."),
    "indite": ("Indite", "common", "laser", "Low value."),
    "gallite": ("Gallite", "common", "laser", "Low value."),
    "coltan": ("Coltan", "common", "laser", "Low value."),
    "uraninite": ("Uraninite", "common", "laser", "Low value."),
    "lepidolite": ("Lepidolite", "common", "laser", "Low value."),
    "cobalt": ("Cobalt", "common", "laser", "Low value."),
    "bauxite": ("Bauxite", "common", "laser", "Very low value."),
}

_TIER_RANK = {"hot": 0, "good": 1, "fuel": 1, "common": 3}


def classify_ore(name: str, localised: str = "") -> dict | None:
    for cand in (_sq(localised), _sq(name)):
        if cand in _MINABLES:
            disp, tier, method, note = _MINABLES[cand]
            return {"display": disp, "tier": tier, "method": method, "note": note,
                    "valuable": tier in ("hot", "good", "fuel")}
    return None


class MiningEngine:
    def __init__(self, state) -> None:
        self.state = state
        self._lock = threading.Lock()
        self._last: dict | None = None          # last ProspectedAsteroid
        self._refined: dict[str, int] = {}      # ore -> tons refined this session
        self._prospected = 0
        self._content = {"High": 0, "Medium": 0, "Low": 0}
        self._cracked = 0
        self._prospectors = 0
        self._collectors = 0
        self._started: float | None = None
        self._sell = {"status": "idle", "commodity": "", "stops": [], "error": ""}
        state.add_journal_listener(self.on_journal)

    def _touch(self) -> None:
        if self._started is None:
            self._started = time.time()

    def on_journal(self, e: dict) -> None:
        ev = e.get("event")
        with self._lock:
            if ev == "ProspectedAsteroid":
                self._touch()
                self._prospected += 1
                content = _strip_sym(e.get("Content") or "")
                content = content.replace("AsteroidMaterialContent_", "")
                if content in self._content:
                    self._content[content] += 1
                mats = []
                for m in e.get("Materials") or []:
                    disp = m.get("Name_Localised") or _strip_sym(m.get("Name"))
                    info = classify_ore(m.get("Name"), m.get("Name_Localised"))
                    mats.append({"name": disp, "proportion": round(float(m.get("Proportion") or 0), 1),
                                 "tier": (info or {}).get("tier"),
                                 "valuable": bool(info and info["valuable"])})
                mats.sort(key=lambda x: -x["proportion"])
                mother = e.get("MotherlodeMaterial")
                mother_info = classify_ore(mother, e.get("MotherlodeMaterial_Localised")) if mother else None
                self._last = {
                    "content": content or "?", "remaining": e.get("Remaining"),
                    "materials": mats,
                    "motherlode": (mother_info or {}).get("display") if mother_info
                    else (e.get("MotherlodeMaterial_Localised") or _strip_sym(mother) if mother else None),
                    "motherlode_valuable": bool(mother_info and mother_info["valuable"]),
                }
            elif ev == "MiningRefined":
                self._touch()
                info = classify_ore(e.get("Type"), e.get("Type_Localised"))
                key = (info or {}).get("display") or e.get("Type_Localised") or _strip_sym(e.get("Type"))
                self._refined[key] = self._refined.get(key, 0) + 1
            elif ev == "LaunchDrone":
                t = str(e.get("Type") or "")
                if t == "Prospector":
                    self._touch(); self._prospectors += 1
                elif t == "Collector":
                    self._touch(); self._collectors += 1
            elif ev == "AsteroidCracked":
                self._touch(); self._cracked += 1

    # --- actions -----------------------------------------------------------
    def reset_session(self) -> None:
        with self._lock:
            self._refined = {}
            self._prospected = self._cracked = self._prospectors = self._collectors = 0
            self._content = {"High": 0, "Medium": 0, "Low": 0}
            self._started = None

    def find_sales(self, commodity: str) -> None:
        commodity = str(commodity or "").strip()
        if not commodity:
            return
        with self._lock:
            self._sell = {"status": "searching", "commodity": commodity, "stops": [], "error": ""}
        threading.Thread(target=self._run_sales, args=(commodity,),
                         name="eliteops-mining-sell", daemon=True).start()

    def _run_sales(self, commodity: str) -> None:
        try:
            ref = self.state.snapshot().get("system")
            if not ref:
                raise ValueError("No reference system — jump in-game first.")
            stops = spansh_client.find_commodity_sales(ref, commodity, 100, large_pad_only=False)
            with self._lock:
                self._sell = {"status": "ready", "commodity": commodity, "stops": stops,
                              "error": "", "reference": ref}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._sell = {"status": "error", "commodity": commodity, "stops": [], "error": str(exc)}

    # --- snapshot ----------------------------------------------------------
    def _cargo(self) -> dict:
        snap = self.state.snapshot()
        cap = snap.get("cargo_capacity")
        limpets, ore, other, used = 0, [], [], 0
        for it in snap.get("cargo") or []:
            name = it.get("Name", "")
            count = int(it.get("Count") or 0)
            used += count
            if _sq(name) == "drones" or _sq(it.get("Name_Localised")) == "limpet":
                limpets += count
                continue
            disp = it.get("Name_Localised") or _strip_sym(name)
            info = classify_ore(name, it.get("Name_Localised"))
            row = {"name": (info or {}).get("display") or disp, "count": count,
                   "tier": (info or {}).get("tier"), "valuable": bool(info and info["valuable"])}
            (ore if info else other).append(row)
        ore.sort(key=lambda x: (_TIER_RANK.get(x["tier"], 2), -x["count"]))
        return {"limpets": limpets, "capacity": cap, "used": used,
                "ore": ore, "other": other,
                "ore_tons": sum(o["count"] for o in ore)}

    def _tips(self, cargo: dict) -> list[str]:
        tips = []
        lim, cap, used = cargo["limpets"], cargo["capacity"], cargo["used"]
        if lim == 0:
            tips.append("No limpets aboard — buy the ‘Limpets’ commodity at a station; each prospector/collector you fire uses one.")
        elif lim < 6:
            tips.append(f"Only {lim} limpets left — you'll run dry soon; restock at a station.")
        if cap and used >= cap:
            tips.append("Hold is FULL — head to a station and sell your ore.")
        elif cap and used / cap >= 0.85:
            tips.append(f"Hold {round(100*used/cap)}% full — start planning your sell run.")
        last = self._last
        if not self._prospected:
            tips.append("Fire a Prospector Limpet at an asteroid to scan what's inside — content High/Medium/Low and the ores it holds.")
        elif last:
            if last.get("motherlode"):
                tips.append(f"That's a CORE asteroid (motherlode: {last['motherlode']}). Shoot the surface deposits, drop a Seismic Charge in each blue fissure, then crack it and collect the chunks.")
            valn = [m["name"] for m in last["materials"] if m["valuable"]][:3]
            if valn:
                tips.append(f"Last asteroid has {', '.join(valn)} — worth mining. Fire mining lasers at the rock and let collector limpets scoop the fragments.")
            elif last.get("content") == "Low":
                tips.append("Low content and nothing valuable — prospect another asteroid.")
        if not tips:
            tips.append("Looking good — keep prospecting and mine the High-content rocks.")
        return tips

    def snapshot(self) -> dict:
        with self._lock:
            cargo = self._cargo()
            refined = sorted(({"name": k, "tons": v, "tier": (classify_ore(k, k) or {}).get("tier")}
                              for k, v in self._refined.items()), key=lambda x: -x["tons"])
            elapsed = int(time.time() - self._started) if self._started else 0
            return {
                "cargo": cargo,
                "last_asteroid": self._last,
                "session": {
                    "refined": refined,
                    "total_tons": sum(self._refined.values()),
                    "prospected": self._prospected, "content": self._content,
                    "cracked": self._cracked, "prospectors": self._prospectors,
                    "collectors": self._collectors, "elapsed": elapsed,
                    "active": self._started is not None,
                },
                "tips": self._tips(cargo),
                "sell": self._sell,
            }
