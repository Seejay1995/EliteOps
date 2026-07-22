# EliteOps ship-build schema — `eliteops.shipbuild/1`

A hand-authorable, importable JSON description of a ship build (a *target* loadout
you want to acquire + engineer). Superset of the old Shipwright `ShipBuildDefinition`,
with **string enums** and coriolis ship/module keys. Live tracking state
(acquired / engineered / current grade) is derived at runtime and is **not** stored
in the authored file.

```json
{
  "schema": "eliteops.shipbuild/1",
  "name": "Caspian Exobiology Explorer",
  "ship": "anaconda",              // coriolis ship key (see data/ships.json). null for a theorized hull.
  "hull_name": null,               // free-text hull name when "ship" is null (theory-crafting)
  "role": "Exobiology Explorer",
  "notes": "",
  "search": {
    "referenceSystem": "",
    "useCurrentSystem": true,
    "radiusLy": 300,
    "requireLargePad": true,
    "includeFleetCarriers": false
  },
  "rollProfile": { "1": 1, "2": 2, "3": 4, "4": 6, "5": 10 },
  "modules": [
    {
      "slot": "FrameShiftDrive",       // optional: target ship slot; else matched by category+size
      "category": "core",              // core | optional | hardpoint | utility | bulkheads
      "type": "Frame Shift Drive",     // module display name (data/modules.json "name")
      "grp": "fsd",                    // optional: coriolis group code (authoritative if present)
      "class": 8,
      "rating": "A",
      "blueprint": "Increased Range",  // optional
      "grade": 5,                       // optional (1-5)
      "experimental": "Mass Manager",  // optional
      "quantity": 1,
      "acquisition": "StandardOutfitting"
    }
  ]
}
```

## Enums

- **category**: `core`, `optional`, `hardpoint`, `utility`, `bulkheads`
- **acquisition**: `Automatic`, `Shipyard`, `StandardOutfitting`, `GuardianTechBroker`,
  `HumanTechBroker`, `EngineerWorkshop`, `Powerplay`, `PreEngineered`, `MissionReward`

## Notes

- `ship` should be a coriolis key from `data/ships.json` (e.g. `anaconda`, `panthermkii`).
  The catalog also fuzzy-resolves the in-game `Ship` name from the journal.
- `grp` (coriolis group code) is authoritative for a module when present; otherwise
  `type` is fuzzy-resolved to a module via `catalog.find_module_by_name`.
- Blueprint names may be authored in short form ("Increased Range"); the catalog's
  dropdowns use the recipe catalog's verbose names ("Increased FSD Range"). Reconciling
  the two exactly is the job of the engineering phase (5d).
- `tools/convert_shipwright.py` upgrades an old `*.shipwright.json` (int `Kind`/`AcquisitionMethod`
  enums, free-text `HullName`) into this schema.
