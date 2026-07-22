"""Shipwright engine for EliteOps.

Live current-ship (from Loadout) + engineering materials (from Materials) +
engineer unlocks (from EngineerProgress), plus an active *build definition*
(eliteops.shipbuild/1) that can be imported/edited/exported and reconciled against
the ship you're actually flying. Bundled catalog via eliteops.catalog.

Foundation cut: build editor + live view. Full material/acquisition planning is 5d/5e.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from . import catalog

_BUILDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "builds")

_SIZE_RE = re.compile(r"size(\d+)")
_CLASS_RE = re.compile(r"class(\d+)")

_VALID_CATEGORIES = {"core", "optional", "hardpoint", "utility", "bulkheads"}
_VALID_ACQ = {"Automatic", "Shipyard", "StandardOutfitting", "GuardianTechBroker",
              "HumanTechBroker", "EngineerWorkshop", "Powerplay", "PreEngineered",
              "MissionReward"}


def _prettify(item: str) -> str:
    s = re.sub(r"_size\d+|_class\d+", "", str(item or ""))
    for p in ("int_", "hpt_"):
        if s.startswith(p):
            s = s[len(p):]
    return " ".join(w.capitalize() for w in s.split("_")) or item


def _tokens(v: Any) -> set[str]:
    return {w for w in "".join(c if c.isalnum() else " " for c in str(v or "").lower()).split()
            if w not in ("fsd", "the", "weapon", "drive", "tuning", "mount")}


class ShipwrightEngine:
    def __init__(self, state) -> None:
        self.state = state
        self.cat = catalog.get()
        self._lock = threading.Lock()
        self._loadout: dict[str, Any] | None = None
        self._materials = {"raw": {}, "manufactured": {}, "encoded": {}}
        self._engineers: dict[str, dict] = {}
        self._stored_modules: list[dict] = []
        self._build: dict[str, Any] | None = None
        self._acq: dict[str, Any] = {"status": "idle", "stops": [], "unresolved": [], "error": ""}
        state.add_journal_listener(self.on_journal)

    # --- journal ingest -----------------------------------------------------
    def on_journal(self, e: dict[str, Any]) -> None:
        ev = e.get("event")
        with self._lock:
            if ev == "Loadout":
                self._loadout = self._parse_loadout(e)
            elif ev == "Materials":
                for cat_key in ("Raw", "Manufactured", "Encoded"):
                    bucket = {}
                    for it in e.get(cat_key, []) or []:
                        if it.get("Name"):
                            bucket[it["Name"].lower()] = int(it.get("Count", 0))
                    self._materials[cat_key.lower()] = bucket
            elif ev == "MaterialCollected":
                self._adjust_material(e.get("Category"), e.get("Name"), int(e.get("Count", 0)))
            elif ev == "MaterialDiscarded":
                self._adjust_material(e.get("Category"), e.get("Name"), -int(e.get("Count", 0)))
            elif ev == "EngineerProgress":
                for en in e.get("Engineers", []) or []:
                    name = en.get("Engineer")
                    if name:
                        self._engineers[name] = {"progress": en.get("Progress"),
                                                 "rank": en.get("Rank", 0)}
            elif ev == "StoredModules":
                self._stored_modules = e.get("Items", []) or []

    def _adjust_material(self, category: Any, name: Any, delta: int) -> None:
        if not name:
            return
        key = str(category or "").lower()
        if key not in self._materials:
            key = "raw"
        bucket = self._materials.setdefault(key, {})
        bucket[str(name).lower()] = max(0, bucket.get(str(name).lower(), 0) + delta)

    def _parse_loadout(self, e: dict[str, Any]) -> dict[str, Any]:
        modules = []
        for m in e.get("Modules", []) or []:
            item = m.get("Item", "")
            grp = self.cat.grp_from_symbol(item)
            cat_mod = self.cat.module(grp) if grp else None
            size = _SIZE_RE.search(item)
            eng = m.get("Engineering")
            engineering = None
            if eng:
                engineering = {
                    "blueprint": eng.get("BlueprintName"),
                    "grade": eng.get("Level"),
                    "quality": eng.get("Quality"),
                    "experimental": eng.get("ExperimentalEffect"),
                    "engineer": eng.get("Engineer"),
                }
            modules.append({
                "slot": m.get("Slot"), "item": item, "grp": grp,
                "name": cat_mod["name"] if cat_mod else _prettify(item),
                "category": cat_mod["category"] if cat_mod else None,
                "size": int(size.group(1)) if size else None,
                "on": m.get("On", True), "value": m.get("Value"),
                "engineering": engineering,
            })
        ship_key = self.cat.ship_key_from_journal(e.get("Ship", ""))
        ship = self.cat.ship(ship_key) if ship_key else None
        return {
            "ship_key": ship_key, "ship": e.get("Ship"),
            "name": (ship or {}).get("name") or e.get("Ship"),
            "ident": e.get("ShipIdent"), "ship_id": e.get("ShipID"),
            "hull_value": e.get("HullValue"), "modules_value": e.get("ModulesValue"),
            "rebuy": e.get("Rebuy"), "unladen_mass": e.get("UnladenMass"),
            "max_jump": e.get("MaxJumpRange"), "cargo_capacity": e.get("CargoCapacity"),
            "fuel_capacity": (e.get("FuelCapacity") or {}).get("Main"),
            "modules": modules,
        }

    # --- build editing ------------------------------------------------------
    def load_build(self, data: dict[str, Any]) -> dict[str, Any]:
        build = self._normalize_build(data)
        with self._lock:
            self._build = build
        return build

    def new_build(self, ship_key: str | None) -> dict[str, Any]:
        ship = self.cat.ship(ship_key) if ship_key else None
        with self._lock:
            self._build = {
                "schema": "eliteops.shipbuild/1",
                "name": (ship or {}).get("name", "New build") if ship else "New build",
                "ship": ship_key if ship else None, "hull_name": None,
                "role": "", "notes": "",
                "search": {"referenceSystem": "", "useCurrentSystem": True,
                           "radiusLy": 250, "requireLargePad": True, "includeFleetCarriers": False},
                "rollProfile": {"1": 1, "2": 2, "3": 4, "4": 6, "5": 10},
                "modules": [],
            }
            return self._build

    def edit_module(self, index: int, patch: dict[str, Any]) -> None:
        with self._lock:
            if not self._build:
                return
            mods = self._build["modules"]
            if index == -1:  # append
                mods.append(self._blank_module())
                index = len(mods) - 1
            if 0 <= index < len(mods):
                mods[index].update({k: patch[k] for k in patch if k in self._blank_module()})
                # keep grp/category in sync when type resolves
                if patch.get("type") and not patch.get("grp"):
                    m = self.cat.find_module_by_name(patch["type"])
                    if m:
                        mods[index]["grp"] = m["grp"]
                        mods[index]["category"] = m["category"]

    def remove_module(self, index: int) -> None:
        with self._lock:
            if self._build and 0 <= index < len(self._build["modules"]):
                self._build["modules"].pop(index)

    @staticmethod
    def _blank_module() -> dict[str, Any]:
        return {"slot": None, "category": "optional", "type": None, "grp": None,
                "class": None, "rating": None, "blueprint": None, "grade": None,
                "experimental": None, "quantity": 1, "acquisition": "StandardOutfitting"}

    def _normalize_build(self, data: dict[str, Any]) -> dict[str, Any]:
        mods = []
        for m in (data.get("modules") or []):
            row = self._blank_module()
            row.update({k: m.get(k) for k in row if k in m})
            if not row.get("grp") and row.get("type"):
                found = self.cat.find_module_by_name(row["type"])
                if found:
                    row["grp"] = found["grp"]
                    row["category"] = found["category"]
            if row.get("grp") and row.get("blueprint"):
                row["blueprint"] = self.cat.resolve_blueprint(row["grp"], row["blueprint"])
            if row.get("category") not in _VALID_CATEGORIES:
                row["category"] = "optional"
            if row.get("acquisition") not in _VALID_ACQ:
                row["acquisition"] = "StandardOutfitting"
            mods.append(row)
        return {
            "schema": "eliteops.shipbuild/1",
            "name": data.get("name") or "Imported build",
            "ship": data.get("ship"), "hull_name": data.get("hull_name"),
            "role": data.get("role") or "", "notes": data.get("notes") or "",
            "search": data.get("search") or {"useCurrentSystem": True, "radiusLy": 250,
                                             "requireLargePad": True, "includeFleetCarriers": False},
            "rollProfile": data.get("rollProfile") or {"1": 1, "2": 2, "3": 4, "4": 6, "5": 10},
            "modules": mods,
        }

    # --- reconciliation (best-effort) --------------------------------------
    def _reconcile(self) -> list[dict[str, Any]]:
        if not self._build:
            return []
        installed = {}
        for m in (self._loadout or {}).get("modules", []):
            if m.get("grp"):
                installed.setdefault(m["grp"], []).append(m)
        out = []
        for m in self._build["modules"]:
            grp = m.get("grp")
            fit = installed.get(grp, [])
            is_installed = bool(fit)
            engineered = False
            if is_installed and m.get("blueprint"):
                want = _tokens(m["blueprint"])
                engineered = any(m2.get("engineering") and (want & _tokens(m2["engineering"].get("blueprint")))
                                 for m2 in fit)
            elif is_installed and not m.get("blueprint"):
                engineered = True  # nothing to engineer
            status = ("installed" if is_installed and (engineered or not m.get("blueprint"))
                      else "needs-engineering" if is_installed else "needs-buy")
            out.append({"type": m.get("type"), "grp": grp, "class": m.get("class"),
                        "rating": m.get("rating"), "status": status,
                        "installed": is_installed, "engineered": engineered})
        return out

    # --- engineering material plan (port of EngineeringMaterialPlanner) ----
    def _material_owned(self, fd: str) -> int:
        key = str(fd or "").lower()
        return sum(bucket.get(key, 0) for bucket in self._materials.values())

    def _plan_materials(self) -> dict[str, Any]:
        if not self._build:
            return {"materials": [], "totalNeeded": 0, "totalMissing": 0,
                    "readyCount": 0, "unmapped": []}
        roll = self._build.get("rollProfile") or {"1": 1, "2": 2, "3": 4, "4": 6, "5": 10}
        need: dict[str, dict] = {}
        unmapped: list[str] = []

        def add(recipe: dict, mult: int, used_by: str) -> None:
            for ing in recipe.get("Ingredients", []) or []:
                fd = (ing.get("FdName") or "").lower()
                if not fd:
                    continue
                slot = need.setdefault(fd, {"fd": fd, "name": ing.get("Name"),
                                            "category": ing.get("Category"), "needed": 0, "usedBy": set()})
                slot["needed"] += int(ing.get("Amount", 0)) * mult
                slot["usedBy"].add(used_by)

        for m in self._build["modules"]:
            grp = m.get("grp")
            qty = int(m.get("quantity") or 1)
            label = m.get("type") or (grp or "?")
            if grp and m.get("blueprint") and m.get("grade"):
                target = int(m["grade"])
                for g in range(1, target + 1):
                    r = self.cat.recipe_for(grp, m["blueprint"], g)
                    if not r:
                        unmapped.append(f"{label}: {m['blueprint']} G{g}")
                        continue
                    add(r, int(roll.get(str(g), 1)) * qty, f"{label} {m['blueprint']}")
            if grp and m.get("experimental"):
                r = self.cat.recipe_for(grp, m["experimental"], -1)
                if r:
                    add(r, qty, f"{label} {m['experimental']}")
                else:
                    unmapped.append(f"{label}: {m['experimental']} (experimental)")

        materials = []
        total_needed = total_missing = ready = 0
        for slot in need.values():
            owned = self._material_owned(slot["fd"])
            missing = max(0, slot["needed"] - owned)
            total_needed += slot["needed"]
            total_missing += missing
            if missing == 0:
                ready += 1
            materials.append({"fd": slot["fd"], "name": slot["name"], "category": slot["category"],
                              "needed": slot["needed"], "owned": owned, "missing": missing,
                              "usedBy": sorted(slot["usedBy"])})
        # sort: missing first (most short), then category, then name
        materials.sort(key=lambda x: (-x["missing"], x["category"] or "", x["name"] or ""))
        return {"materials": materials, "totalNeeded": total_needed, "totalMissing": total_missing,
                "readyCount": ready, "total": len(materials), "unmapped": unmapped}

    # --- saved builds -------------------------------------------------------
    @staticmethod
    def _slug(name: str) -> str:
        s = "".join(c if c.isalnum() else "-" for c in str(name or "").lower()).strip("-")
        return re.sub(r"-+", "-", s) or "build"

    def list_builds(self) -> list[dict]:
        out = []
        try:
            for fn in os.listdir(_BUILDS_DIR):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(_BUILDS_DIR, fn), encoding="utf-8") as fh:
                        b = json.load(fh)
                    out.append({"slug": fn[:-5], "name": b.get("name") or fn[:-5],
                                "ship": b.get("ship"), "modules": len(b.get("modules") or [])})
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        out.sort(key=lambda x: x["name"].lower())
        return out

    def save_build(self, name: str | None = None) -> dict:
        with self._lock:
            if not self._build:
                raise ValueError("No build to save.")
            if name:
                self._build["name"] = name
            slug = self._slug(self._build.get("name"))
            os.makedirs(_BUILDS_DIR, exist_ok=True)
            with open(os.path.join(_BUILDS_DIR, slug + ".json"), "w", encoding="utf-8") as fh:
                json.dump(self._build, fh, indent=2)
            return {"slug": slug, "name": self._build.get("name")}

    def load_saved(self, slug: str) -> dict | None:
        path = os.path.join(_BUILDS_DIR, self._slug(slug) + ".json")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return self.load_build(json.load(fh))

    def delete_build(self, slug: str) -> None:
        path = os.path.join(_BUILDS_DIR, self._slug(slug) + ".json")
        try:
            os.remove(path)
        except OSError:
            pass

    # --- engineer plan (bundled + live unlock status) ----------------------
    def _engineer_plan(self) -> list[dict[str, Any]]:
        if not self._build:
            return []
        out, seen = [], set()
        for m in self._build["modules"]:
            bp, grade = m.get("blueprint"), m.get("grade")
            if not bp:
                continue  # experimentals roll at whoever does the blueprint; not listed separately
            key = (m.get("type"), bp)
            if key in seen:
                continue
            seen.add(key)
            g = int(grade) if isinstance(grade, int) and grade > 0 else 1
            cands = self.cat.engineers_for(m.get("type") or "", bp, g)
            engineers, any_unlocked = [], False
            for e in cands:
                prog = self._engineers.get(e["name"])
                unlocked = bool(prog and (prog.get("progress") == "Unlocked" or (prog.get("rank") or 0) > 0))
                any_unlocked = any_unlocked or unlocked
                engineers.append({"name": e["name"], "unlocked": unlocked,
                                  "permit": e.get("permit", False), "system": e.get("system"),
                                  "base": e.get("base"), "planet": e.get("planet")})
            out.append({"module": m.get("type"), "blueprint": bp,
                        "grade": grade if isinstance(grade, int) else None,
                        "experimental": bool(m.get("experimental")),
                        "engineers": engineers, "anyUnlocked": any_unlocked,
                        "unresolved": not engineers})
        return out

    # --- acquisition (Spansh station sourcing, on demand) ------------------
    @staticmethod
    def _mod_label(m: dict[str, Any]) -> str:
        cr = f"{m.get('class') or ''}{m.get('rating') or ''}".strip()
        return (cr + " " + (m.get("type") or "?")).strip()

    @staticmethod
    def _spansh_module_name(m: dict[str, Any]) -> str:
        remap = {"Guardian FSD Booster": "Guardian Frame Shift Drive Booster",
                 "Surface Scanner": "Detailed Surface Scanner"}
        return remap.get(m.get("type"), m.get("type") or "")

    def plan_acquisition(self) -> None:
        with self._lock:
            if not self._build:
                raise ValueError("Load a build first.")
            self._acq = {"status": "searching", "stops": [], "unresolved": [], "error": ""}
        threading.Thread(target=self._run_acquire, name="eliteops-acquire", daemon=True).start()

    def _run_acquire(self) -> None:
        import spansh_client as sc  # vendored (sys.path set by catalog import)
        try:
            with self._lock:
                build = json.loads(json.dumps(self._build))  # cheap deep copy
                recon = self._reconcile()
                current_ship_key = (self._loadout or {}).get("ship_key")
            search = build.get("search") or {}
            ref = (self.state.snapshot().get("system")
                   if search.get("useCurrentSystem", True) else search.get("referenceSystem"))
            ref = ref or self.state.snapshot().get("system")
            if not ref:
                raise ValueError("No reference system — jump in-game or set one in the build.")
            radius = float(search.get("radiusLy") or 100)
            large = bool(search.get("requireLargePad", True))
            carriers = bool(search.get("includeFleetCarriers", False))

            station_map: dict[tuple, dict] = {}
            unresolved: list[str] = []

            def add_station(s: dict, label: str) -> None:
                key = (s["system"], s["station"])
                slot = station_map.setdefault(key, {"info": s, "covers": set()})
                slot["covers"].add(label)

            needed = [m for i, m in enumerate(build["modules"])
                      if i < len(recon) and recon[i]["status"] == "needs-buy"]
            for m in needed:
                label = self._mod_label(m)
                try:
                    stations = sc.find_module_stations(
                        ref, self._spansh_module_name(m), m.get("class"), m.get("rating"),
                        radius=radius, large_pad=large, include_carriers=carriers)
                except Exception:  # noqa: BLE001
                    stations = []
                if not stations:
                    unresolved.append(label)
                for s in stations:
                    add_station(s, label)

            if build.get("ship") and build["ship"] != current_ship_key:
                ship = self.cat.ship(build["ship"])
                if ship:
                    label = "Hull: " + ship["name"]
                    try:
                        stations = sc.find_ship_stations(ref, ship["name"], radius=radius,
                                                         large_pad=large, include_carriers=carriers)
                    except Exception:  # noqa: BLE001
                        stations = []
                    if not stations:
                        unresolved.append(label)
                    for s in stations:
                        add_station(s, label)

            # greedy set-cover: fewest stops, nearest tie-break
            covered, stops, remaining = set(), [], dict(station_map)
            while True:
                best, best_new, best_dist = None, 0, 1e18
                for key, v in remaining.items():
                    new = len(v["covers"] - covered)
                    dist = v["info"].get("distance_ly") or 1e9
                    if new > best_new or (new == best_new and new > 0 and dist < best_dist):
                        best, best_new, best_dist = key, new, dist
                if not best or best_new == 0:
                    break
                v = remaining.pop(best)
                fresh = sorted(v["covers"] - covered)
                covered |= v["covers"]
                stops.append({"system": v["info"]["system"], "station": v["info"]["station"],
                              "distance_ly": v["info"].get("distance_ly"),
                              "distance_ls": v["info"].get("distance_ls"),
                              "type": v["info"].get("type"), "covers": fresh})
            with self._lock:
                self._acq = {"status": "ready", "stops": stops, "unresolved": unresolved,
                             "error": "", "reference": ref}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._acq = {"status": "error", "stops": [], "unresolved": [], "error": str(exc)}

    # --- snapshot -----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            mats = self._materials
            engineers = sorted(
                ({"name": n, "progress": v.get("progress"), "rank": v.get("rank", 0),
                  "unlocked": v.get("progress") == "Unlocked" or (v.get("rank") or 0) > 0}
                 for n, v in self._engineers.items()),
                key=lambda x: x["name"])
            return {
                "current_ship": self._loadout,
                "materials": {
                    "raw": mats["raw"], "manufactured": mats["manufactured"], "encoded": mats["encoded"],
                    "counts": {"raw": len(mats["raw"]), "manufactured": len(mats["manufactured"]),
                               "encoded": len(mats["encoded"])},
                },
                "engineers": engineers,
                "build": self._build,
                "reconciliation": self._reconcile(),
                "material_plan": self._plan_materials(),
                "engineer_plan": self._engineer_plan(),
                "acquisition": self._acq,
                "saved_builds": self.list_builds(),
            }

    def export_build(self) -> dict[str, Any] | None:
        with self._lock:
            return self._build

    # --- persistence --------------------------------------------------------
    def persist(self) -> dict[str, Any]:
        with self._lock:
            return {"build": self._build}

    def restore(self, data: dict[str, Any] | None) -> None:
        if data and data.get("build"):
            self.load_build(data["build"])
