#!/usr/bin/env python3
"""Convert an old Shipwright build (*.shipwright.json) into EliteOps schema v1.

Usage:  python tools/convert_shipwright.py <in.shipwright.json> [out.json]
Maps int Kind/AcquisitionMethod enums to strings, resolves the free-text HullName
to a coriolis ship key, and resolves each module Name to a catalog module (grp +
category). Prints to stdout if no output path is given.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eliteops.catalog import get  # noqa: E402

_ACQ = {0: "Automatic", 1: "Shipyard", 2: "StandardOutfitting", 3: "GuardianTechBroker",
        4: "HumanTechBroker", 5: "EngineerWorkshop", 6: "Powerplay", 7: "PreEngineered",
        8: "MissionReward", 9: "Automatic"}


def convert(src: dict) -> dict:
    cat = get()
    reqs = src.get("Requirements", []) or []

    # the Kind==0 requirement (or HullName) is the ship
    hull_name = src.get("HullName") or ""
    for r in reqs:
        if r.get("Kind") == 0 and r.get("Name"):
            hull_name = r["Name"]
            break
    ship_key = cat.ship_key_from_journal(hull_name)

    modules = []
    for r in reqs:
        if r.get("Kind") == 0:
            continue  # hull captured above
        name = r.get("Name") or ""
        m = cat.find_module_by_name(name)
        modules.append({
            "slot": None,
            "category": m["category"] if m else "optional",
            "type": m["name"] if m else name,
            "grp": m["grp"] if m else None,
            "class": r.get("ModuleClass"),
            "rating": r.get("Rating"),
            "blueprint": r.get("Blueprint") or None,
            "grade": r.get("Grade") or None,
            "experimental": r.get("ExperimentalEffect") or None,
            "quantity": r.get("Quantity", 1),
            "acquisition": _ACQ.get(r.get("AcquisitionMethod", 0), "Automatic"),
        })

    return {
        "schema": "eliteops.shipbuild/1",
        "name": src.get("Name") or "Imported build",
        "ship": ship_key,
        "hull_name": None if ship_key else (hull_name or None),
        "role": src.get("Role") or "",
        "notes": "",
        "search": {
            "referenceSystem": src.get("ReferenceSystem") or "",
            "useCurrentSystem": bool(src.get("AutoUseCurrentSystem", True)),
            "radiusLy": src.get("SearchRadiusLy", 250),
            "requireLargePad": bool(src.get("RequireLargePad", True)),
            "includeFleetCarriers": bool(src.get("IncludeFleetCarriers", False)),
        },
        "rollProfile": {"1": 1, "2": 2, "3": 4, "4": 6, "5": 10},
        "modules": modules,
    }


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as fh:
        src = json.load(fh)
    result = convert(src)
    text = json.dumps(result, indent=2)
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {sys.argv[2]}  ({len(result['modules'])} modules, ship={result['ship']})")
    else:
        print(text)


if __name__ == "__main__":
    main()
