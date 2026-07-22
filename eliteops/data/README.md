# EliteOps bundled catalog data

Reference data for the Shipwright tool. Built once, then loaded offline at runtime
by `eliteops/catalog.py`. No network access is needed after these files exist.

## Files

- **`ships.json`** — 47 ships with slot layouts (core / hardpoints / utility / optional)
  and base stats. Built from [EDCD/coriolis-data](https://github.com/EDCD/coriolis-data)
  (`ships/*.json`) by `tools/build_catalog.py`.
- **`modules.json`** — 87 module *types*, each tagged with a `category`
  (core / optional / hardpoint / utility) and its available class/rating/mount
  variants (with FDev `symbol` for journal matching). Built from coriolis-data
  `modules/{standard,internal,hardpoints}/*.json`. A module in the `hardpoints`
  folder is `hardpoint` if its variants have a `mount`, else `utility`.
- **`engineering-recipes-v1.json`** — 992 engineering blueprint/grade recipes with
  per-roll material `Ingredients` (`FdName` joins directly to the journal `Materials`
  event `Name`). Copied verbatim from
  `E:\EDDiscovery\Data\AddonFiles\Shipwright\engineering-recipes-v1.json`, itself a
  frozen snapshot of EDDiscovery's `EliteDangerousCore.Recipes.EngineeringRecipes`.
- **`engineers.json`** — 19 engineers (system, base, permit, unlock, capabilities),
  transcribed from `E:\Projects\Shipwright\src\Shipwright.Core\EngineerCatalog.cs`.

## Licensing

coriolis-data is MIT licensed (EDCD). Ship/module reference data is game data
surfaced by the community; used here for a fan tool. Regenerate ships/modules with
`python tools/build_catalog.py`.
