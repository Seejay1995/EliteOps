"""Bundled ship/module/blueprint/engineer catalog for the Shipwright tool.

Loads eliteops/data/*.json once and exposes the lookups the UI + engine need,
notably the CATEGORY-FILTERED module lists that the old EDD plugin lacked (so a
"Hardpoint" dropdown shows only weapons, never FSDs). Standard library only.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# coriolis module group code -> engineering-recipe "Modules" token(s). Only the
# genuinely engineerable groups are mapped; anything unmapped simply has no
# blueprints (fuel tank, docking computer, passenger cabins, most limpets, ...).
GRP_TO_RECIPE: dict[str, list[str]] = {
    # core
    "fsd": ["FSD", "FSD (SCO)"], "t": ["Engine"], "pp": ["Power Plant"],
    "pd": ["Power Distributor"], "ls": ["Life Support"], "s": ["Sensor"],
    # optional
    "am": ["AFM"], "sg": ["Shield Generator"], "bsg": ["Shield Generator"],
    "psg": ["Shield Generator"], "scb": ["Shield Cell Bank", "Shield Cell"],
    "cr": ["Cargo Rack"], "crl": ["Cargo Rack"], "fs": ["Fuel Scoop"],
    "fi": ["FSD Interdictor"], "hr": ["Hull Reinforcement"],
    "ghrp": ["Hull Reinforcement"], "mahr": ["Hull Reinforcement"],
    "mrp": ["Module"], "gmrp": ["Module"], "rf": ["Refineries"],
    "cc": ["Collection Limpet"], "pc": ["Prospecting Limpet"],
    "fx": ["Fuel Transfer Limpet"], "hb": ["Hatch Breaker Limpet"], "fh": ["Fighter"],
    "ss": ["Surface Scanner"],
    # hardpoints
    "bl": ["Beam Laser"], "ul": ["Burst Laser"], "pl": ["Pulse Laser"],
    "c": ["Cannon"], "mc": ["Multicannon"], "advmc": ["Multicannon"],
    "fc": ["Frag Cannon"], "pa": ["Plasma Accelerator"], "rg": ["Rail Gun"],
    "mr": ["Missile"], "amr": ["Missile"], "tp": ["Torpedo"], "ntp": ["Torpedo"],
    "nl": ["Mine"], "ml": ["Mining Laser"], "ggc": ["Gauss Cannon"],
    "gsc": ["Shard Cannon"], "tbsc": ["Shock Cannon"], "tbrfl": ["Flechette"],
    "axmc": ["Anti Thargoid"], "axmce": ["Anti Thargoid"],
    "axmr": ["Anti Thargoid"], "axmre": ["Anti Thargoid"],
    # utility
    "sb": ["Shield Booster"], "hs": ["Heat Sink Launcher"], "ch": ["Chaff Launcher"],
    "ec": ["ECM"], "po": ["Point Defence"], "cs": ["Cargo Scanner"],
    "kw": ["Kill Warrant Scanner"], "ws": ["Wake Scanner"],
}


def _load(name: str) -> Any:
    with open(os.path.join(_DATA, name), encoding="utf-8") as fh:
        return json.load(fh)


class Catalog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ships: list[dict] = _load("ships.json")
        self._modules: list[dict] = _load("modules.json")
        self._engineers: list[dict] = _load("engineers.json")
        self._recipes: list[dict] = _load("engineering-recipes-v1.json")
        self._ship_by_key = {s["key"]: s for s in self._ships}
        self._mod_by_grp = {m["grp"]: m for m in self._modules}
        # symbol-root -> grp, for mapping journal Loadout items (e.g.
        # 'int_hyperdrive_overcharge_size7_class2') back to a catalog group.
        roots: dict[str, str] = {}
        for m in self._modules:
            for v in m["variants"]:
                sym = v.get("symbol")
                if not sym:
                    continue
                root = _symbol_root(sym)
                if root and root not in roots:
                    roots[root] = m["grp"]
        self._roots = sorted(roots.items(), key=lambda kv: -len(kv[0]))
        # token -> recipes, for blueprints_for()
        self._by_token: dict[str, list[dict]] = {}
        for r in self._recipes:
            for tok in (r.get("Modules") or []):
                self._by_token.setdefault(tok, []).append(r)

    # --- ships --------------------------------------------------------------
    def ships(self) -> list[dict]:
        return [{"key": s["key"], "name": s["name"], "manufacturer": s.get("manufacturer"),
                 "pad": s.get("pad")} for s in self._ships]

    def ship(self, key: str) -> dict | None:
        return self._ship_by_key.get(key)

    def ship_key_from_journal(self, journal_ship: str) -> str | None:
        """journal 'Ship' (e.g. 'panthermkii') is already the coriolis key; also
        accept a fuzzy display-name match as a fallback."""
        js = (journal_ship or "").strip().lower()
        if js in self._ship_by_key:
            return js
        for s in self._ships:
            if s["name"].lower().replace(" ", "") == js.replace(" ", ""):
                return s["key"]
        return None

    # --- modules ------------------------------------------------------------
    def categories(self) -> list[str]:
        return ["core", "optional", "hardpoint", "utility", "bulkheads"]

    def modules_by_category(self, category: str) -> list[dict]:
        return sorted(
            ({"grp": m["grp"], "name": m["name"], "category": m["category"],
              "variants": m["variants"], "engineerable": m["grp"] in GRP_TO_RECIPE}
             for m in self._modules if m["category"] == category),
            key=lambda x: x["name"],
        )

    def module(self, grp: str) -> dict | None:
        return self._mod_by_grp.get(grp)

    def grp_from_symbol(self, item: str) -> str | None:
        """Map a journal Loadout `Item` (e.g. 'int_hyperdrive_overcharge_size7_class2')
        to a catalog group code by longest-root substring match."""
        norm = _norm(item)
        for root, grp in self._roots:
            if root and root in norm:
                return grp
        return None

    def find_module_by_name(self, name: str) -> dict | None:
        """Resolve a free-text module name (e.g. from an imported build) to a
        catalog module, tolerant of wording differences ('Guardian Frame Shift
        Drive Booster' -> 'Guardian FSD Booster', 'Detailed Surface Scanner' ->
        'Surface Scanner')."""
        target = _canon(name)
        if not target:
            return None
        best, best_score = None, 0
        for m in self._modules:
            mn = _canon(m["name"])
            if mn == target:
                return m
            if mn in target or target in mn:
                score = min(len(mn), len(target))
                if score > best_score:
                    best, best_score = m, score
        if best:
            return best
        # token overlap fallback (on canonicalised words)
        tw = set(_canon_words(name))
        for m in self._modules:
            overlap = len(tw & set(_canon_words(m["name"])))
            if overlap > best_score:
                best, best_score = m, overlap
        return best

    # --- blueprints ---------------------------------------------------------
    def blueprints_for(self, grp: str) -> dict:
        """Blueprints + experimental effects available for a module group,
        derived from the bundled recipe catalog."""
        tokens = GRP_TO_RECIPE.get(grp, [])
        blueprints: dict[str, int] = {}
        experimentals: set[str] = set()
        for tok in tokens:
            for r in self._by_token.get(tok, []):
                name = r.get("Name")
                grade = r.get("Grade", 0)
                if not name:
                    continue
                if grade == -1:
                    experimentals.add(name)
                elif grade >= 1:
                    blueprints[name] = max(blueprints.get(name, 0), grade)
        return {
            "blueprints": [{"name": n, "maxGrade": g}
                           for n, g in sorted(blueprints.items())],
            "experimentals": sorted(experimentals),
        }

    def recipe_for(self, grp: str, name: str, grade: int) -> dict | None:
        """The recipe (ingredient list) for a blueprint/experimental at a grade on a
        module group. grade -1 = experimental effect."""
        tokens = set(GRP_TO_RECIPE.get(grp, []))
        for r in self._recipes:
            if r.get("Grade") == grade and r.get("Name") == name:
                if not tokens or (set(r.get("Modules") or []) & tokens):
                    return r
        # fallback: name+grade only (module token mismatch)
        for r in self._recipes:
            if r.get("Grade") == grade and r.get("Name") == name:
                return r
        return None

    def resolve_blueprint(self, grp: str, name: str) -> str:
        """Map an authored short blueprint name ('Increased Range') to the catalog's
        recipe name ('Increased FSD Range') for that module group, so imported builds
        select correctly. Returns the original name if nothing matches."""
        if not name:
            return name
        want = _bp_tokens(name)
        if not want:
            return name
        best, best_score = name, 0
        for bp in self.blueprints_for(grp).get("blueprints", []):
            score = len(want & _bp_tokens(bp["name"]))
            if score > best_score:
                best, best_score = bp["name"], score
        return best

    # --- engineers ----------------------------------------------------------
    def engineers(self) -> list[dict]:
        return self._engineers

    def engineers_for(self, module_name: str, blueprint: str, grade: int = 1) -> list[dict]:
        m = _norm(module_name)
        bp_words = _bp_tokens(blueprint)
        out = []
        for e in self._engineers:
            for cap in e.get("capabilities", []):
                cap_words = _bp_tokens(cap["blueprint"])
                module_ok = _norm(cap["module"]) in m or m in _norm(cap["module"])
                if module_ok and (bp_words & cap_words) and cap["maxGrade"] >= grade:
                    out.append(e)
                    break
        out.sort(key=lambda e: (e.get("permit", False), e["name"]))
        return out


def _norm(v: Any) -> str:
    return "".join(ch for ch in str(v or "").lower() if ch.isalnum())


def _words(v: Any) -> list[str]:
    return "".join(c if c.isalnum() else " " for c in str(v or "").lower()).split()


# canonicalise module wording differences before matching (FDev/coriolis spell
# "FSD" out as "Frame Shift Drive" in some names, abbreviate in others)
def _canon_str(v: Any) -> str:
    s = str(v or "").lower()
    s = s.replace("frame shift drive", "fsd").replace("detailed surface scanner", "surface scanner")
    return s


def _canon(v: Any) -> str:
    return _norm(_canon_str(v))


def _canon_words(v: Any) -> list[str]:
    return _words(_canon_str(v))


# module mount/size tokens dropped when reducing a FDev symbol to its identifying root
_SYMBOL_DROP = {"int", "hpt", "fixed", "gimballed", "turret", "tiny", "small",
                "medium", "large", "huge", "standard"}


def _symbol_root(symbol: str) -> str:
    parts = str(symbol or "").lower().split("_")
    keep = [p for p in parts
            if p not in _SYMBOL_DROP and not p.startswith("size") and not p.startswith("class")]
    return "".join(keep)


# filler words that differ between the recipe catalog's verbose blueprint names
# ("Increased FSD Range", "Dirty Drive Tuning") and the engineers' short names
# ("Increased Range", "Dirty"), stripped so a word-overlap match still connects them.
_BP_FILLER = {"fsd", "weapon", "drive", "drives", "tuning", "mount", "sequence",
              "blaster", "sco", "v1", "size", "class", "the", "of"}


def _bp_tokens(name: Any) -> set[str]:
    words = "".join(c if c.isalnum() else " " for c in str(name or "").lower()).split()
    return {w for w in words if w not in _BP_FILLER and not w.isdigit()}


_INSTANCE: Catalog | None = None


def get() -> Catalog:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Catalog()
    return _INSTANCE
