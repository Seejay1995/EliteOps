# EliteOps

A single local web dashboard for **Elite Dangerous** — trade routing, exploration,
exobiology, Guardian unlocks, engineering materials, ship building and community
goals — served to your browser and to any tablet or phone on your network.

**No EDDiscovery required. No pip dependencies. Pure Python standard library.**

It reads your game's journal and sidecar files directly, so it works whether or not
any other tool is running.

![tabs](https://img.shields.io/badge/tabs-10-orange) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none-green)

---

## Quick start

```bash
python run.py
```

Then open **http://localhost:8384**. On Windows just double-click **`EliteOps.bat`** —
it starts the server, opens your browser, and prints the LAN address for a tablet.

From another device on the same Wi-Fi, browse to `http://<your-PC-IP>:8384`
(the launcher prints it). If it won't connect, allow inbound **TCP 8384** through
Windows Firewall on the Private profile.

Requires **Python 3.10+**. Nothing to install.

---

## What's in it

| Tab | What it does |
|---|---|
| **Route** | Generates profit-optimised Spansh trade routes, then walks you through them with a **live checklist that ticks itself** as you dock, sell and buy — and copies the next system to your clipboard automatically. |
| **Navigate** | One clear recommendation at a time (Complete the FSS / Map this body / Cash in / Skip), with an unsold-exploration-value "at risk" meter. Skips telling you to FSS systems that are already fully charted. |
| **System** | An interactive EDD-style system scan: the full body tree with values, landable/terraformable/bio badges, rings, and click-to-expand detail. |
| **Exo** | Spansh exobiology routes plus live per-species sampling progress and session value. |
| **Guardian** | Pick the Guardian modules you want → exactly what each unlock costs, grouped by **how you get it** (Ruins / Structures / farm / buy), diffed against your real inventory, with a Guardian-only Tech Broker finder. |
| **Materials** | Your full engineering-material inventory by category and grade with storage caps, a "where do I get X" lookup, farming guides, and a Material Trader finder. |
| **Shipwright** | Build ships against a real catalog with **type-filtered dropdowns**, save/load named builds, see the engineering materials you still need, which engineers roll each blueprint, and where to buy what you're missing. |
| **Colonisation** | Tracks your construction depot's commodity progress and helps source the haul. |
| **Goals** | Community Goals with a live countdown and your standing — from the journal, or live via INARA. |
| **Firsts** | First discovery / map / footfall radar, with lifetime totals. |

---

## Optional integrations

Both are **off by default** and degrade gracefully — the app is fully usable without them.

- **EDDiscovery bridge** — if EDD is installed, EliteOps reads its `EDDUser.sqlite`
  *read-only* for your full cross-session history (lifetime systems, jumps, scans,
  first discoveries). Missing or locked DB just hides the extra stats.
- **INARA live Community Goals** — paste a free INARA API key on the Goals tab for an
  always-live goal list with objectives and rewards. The key is stored **locally only**
  in `session/config.json` (git-ignored) and is never exposed by the API.

---

## Layout

```
run.py                  launch the server
EliteOps.bat            Windows one-click launcher (+ prints the LAN URL)
eliteops/
  server.py             stdlib HTTP server + JSON API
  state.py              live game state (journal tail + Status/Cargo sidecars)
  *_engine.py           one engine per tab
  catalog.py            bundled ship/module/blueprint/engineer lookups
  data/                 bundled reference data (see data/README.md)
  vendor/               self-contained helpers (Spansh client, value formulas)
web/index.html          the single-page dashboard
tools/                  catalog builder + build converter
examples/               example ship builds you can import
```

Local state lives in `session/` and `builds/` and is git-ignored.

---

## Data & credits

Bundled reference data and its provenance are documented in
[`eliteops/data/README.md`](eliteops/data/README.md):

- Ship and module catalogue derived from [EDCD/coriolis-data](https://github.com/EDCD/coriolis-data) (MIT).
- Engineering blueprint recipes from EDDiscovery's recipe catalogue.
- Material-trader / tech-broker type rules from [EDCD/FDevIDs](https://github.com/EDCD/FDevIDs).
- Route, station and commodity search via [Spansh](https://spansh.co.uk/).
- Community Goals via your journal and optionally [INARA](https://inara.cz/).

An unofficial fan tool. Elite Dangerous is © Frontier Developments plc.
Not affiliated with Frontier, EDCD, Spansh or INARA.
