#!/usr/bin/env python3
"""Build EliteOps' bundled ship/module catalog from EDCD/coriolis-data (MIT).

Run once (needs internet):  python tools/build_catalog.py
Writes eliteops/data/ships.json + modules.json (trimmed for our needs) and copies
the engineering recipe catalog. Runtime never touches the network after this.

coriolis-data layout:
  ships/<key>.json          -> { "<key>": { properties, slots{standard,hardpoints,internal}, bulkheads } }
  modules/standard/*.json   -> core internals   (category "core")
  modules/internal/*.json   -> optional internals(category "optional")
  modules/hardpoints/*.json -> weapons (have "mount") + utility (no "mount")
Each module file is { "<grp>": [ {class, rating, mount?, symbol, cost, mass, ...}, ... ] }.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.parse
import urllib.request

REPO = "EDCD/coriolis-data"
RAW = "https://raw.githubusercontent.com/" + REPO + "/master/"
API = "https://api.github.com/repos/" + REPO + "/git/trees/master:"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "eliteops", "data")
RECIPE_SRC = r"E:\EDDiscovery\Data\AddonFiles\Shipwright\engineering-recipes-v1.json"

# coriolis "standard" slot array is positional — this is the canonical core order.
CORE_SLOTS = ["PowerPlant", "Thrusters", "FrameShiftDrive", "LifeSupport",
              "PowerDistributor", "Sensors", "FuelTank"]

_ACRONYMS = {"ax": "AX", "fsd": "FSD", "afmu": "AFMU", "scb": "SCB", "ecm": "ECM",
             "mk": "Mk", "nx": "NX"}


def _get(url: str, tries: int = 4):
    req = urllib.request.Request(url, headers={"User-Agent": "EliteOps-catalog-build/1.0"})
    for attempt in range(tries):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            time.sleep(1.5)


def _tree(path: str) -> list[str]:
    data = _get(API + urllib.parse.quote(path))
    return [t["path"] for t in data["tree"] if t["path"].endswith(".json")]


def _title(filename: str) -> str:
    stem = filename[:-5].replace("_", " ")
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in stem.split())


def build_modules() -> list[dict]:
    out: list[dict] = []
    dirs = {"standard": "core", "internal": "optional", "hardpoints": None}
    for folder, fixed_cat in dirs.items():
        for fname in _tree("modules/" + folder):
            if "missing" in fname:
                continue
            doc = _get(RAW + "modules/" + folder + "/" + fname)
            display = _title(fname)
            for grp, records in doc.items():
                if not isinstance(records, list):
                    continue
                variants = []
                has_mount = False
                for r in records:
                    if not isinstance(r, dict) or "class" not in r:
                        continue
                    mount = r.get("mount")
                    if mount:
                        has_mount = True
                    variants.append({
                        "class": r.get("class"), "rating": r.get("rating"),
                        "mount": mount, "symbol": r.get("symbol"),
                        "cost": r.get("cost"), "mass": r.get("mass"),
                        "integrity": r.get("integrity"), "power": r.get("power"),
                    })
                if not variants:
                    continue
                if fixed_cat:
                    category = fixed_cat
                else:  # hardpoints folder: weapons have a mount, utility do not
                    category = "hardpoint" if has_mount else "utility"
                out.append({"grp": grp, "category": category, "name": display,
                            "variants": variants})
    return out


def build_ships() -> list[dict]:
    out: list[dict] = []
    for fname in _tree("ships"):
        doc = _get(RAW + "ships/" + fname)
        for key, s in doc.items():
            if not isinstance(s, dict) or "slots" not in s:
                continue
            p = s.get("properties", {})
            slots = s["slots"]
            std = slots.get("standard", [])
            core = [{"slot": CORE_SLOTS[i], "size": std[i]}
                    for i in range(min(len(CORE_SLOTS), len(std)))]
            hp = slots.get("hardpoints", [])
            hardpoints = [x for x in hp if isinstance(x, int) and x > 0]
            utility = sum(1 for x in hp if x == 0)
            optional = []
            for it in slots.get("internal", []):
                if isinstance(it, int):
                    optional.append({"size": it, "restrict": None})
                elif isinstance(it, dict):
                    optional.append({"size": it.get("class"),
                                     "restrict": it.get("name")})
            bulkheads = [b.get("name") for b in s.get("bulkheads", [])
                         if isinstance(b, dict)]
            out.append({
                "key": key,
                "name": p.get("name", key),
                "manufacturer": p.get("manufacturer"),
                "pad": p.get("class"),
                "stats": {
                    "hullMass": p.get("hullMass"), "speed": p.get("speed"),
                    "boost": p.get("boost"), "baseArmour": p.get("baseArmour"),
                    "baseShield": p.get("baseShieldStrength"),
                    "hardness": p.get("hardness"), "crew": p.get("crew"),
                    "hullCost": p.get("hullCost"),
                },
                "slots": {"core": core, "hardpoints": hardpoints,
                          "utility": utility, "optional": optional},
                "bulkheads": bulkheads,
            })
    out.sort(key=lambda x: x["name"])
    return out


def main() -> None:
    os.makedirs(DATA, exist_ok=True)
    print("Fetching modules...")
    modules = build_modules()
    print(f"  {len(modules)} module types")
    print("Fetching ships...")
    ships = build_ships()
    print(f"  {len(ships)} ships")

    with open(os.path.join(DATA, "modules.json"), "w", encoding="utf-8") as fh:
        json.dump(modules, fh, separators=(",", ":"))
    with open(os.path.join(DATA, "ships.json"), "w", encoding="utf-8") as fh:
        json.dump(ships, fh, separators=(",", ":"))

    if os.path.isfile(RECIPE_SRC):
        shutil.copyfile(RECIPE_SRC, os.path.join(DATA, "engineering-recipes-v1.json"))
        print("  copied engineering-recipes-v1.json")
    else:
        print(f"  WARN: recipe source not found at {RECIPE_SRC}")

    # sanity report
    cats: dict[str, int] = {}
    for m in modules:
        cats[m["category"]] = cats.get(m["category"], 0) + 1
    print("categories:", cats)
    print("done ->", DATA)


if __name__ == "__main__":
    main()
