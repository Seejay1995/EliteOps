"""EDSM fallback for the Material Trader / Guardian Broker finders.

Inara has the best 'nearest station with service X' tool, but it's a web scrape
and Inara throttles/blocks bursts. EDSM has a proper JSON API whose per-station
`otherServices` list DOES record "Material Trader" / "Technology Broker" (unlike
Spansh, whose services list is empty) — it just has no 'nearest with service'
query, so we pull the nearest populated systems and check their stations in
parallel. Slower than Inara, but reliable and API-legit. Standard library only.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_UA = "EliteOps/1.0 (+material finder fallback)"
_SPHERE = "https://www.edsm.net/api-v1/sphere-systems"
_STATIONS = "https://www.edsm.net/api-system-v1/stations"
_MARKET = "https://www.edsm.net/api-system-v1/stations/market"

# EDCD rule: a Material Trader's TYPE follows the station economy (secondary overrides primary).
_MAT_RULE = {"High Tech": "encoded", "Military": "encoded",
             "Extraction": "raw", "Refinery": "raw", "Industrial": "manufactured"}

_CACHE: dict[tuple, tuple[float, Any]] = {}
_CACHE_TTL = 600.0  # 10 min
_LOCK = threading.Lock()


def _get(url: str, params: dict, timeout: float):
    u = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(u, headers={"User-Agent": _UA})
    last = None
    for attempt in range(2):  # EDSM can be slow under burst — one retry
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            if attempt == 0:
                time.sleep(0.6)
    raise last  # type: ignore[misc]


def _mat_type(econ: str, econ2: str) -> str | None:
    # The trader type follows the PRIMARY economy (verified against Inara's own
    # classifications: High Tech->Encoded, Industrial->Manufactured, Refinery->Raw);
    # fall back to the secondary only if the primary economy has no trader mapping.
    if econ in _MAT_RULE:
        return _MAT_RULE[econ]
    if econ2 in _MAT_RULE:
        return _MAT_RULE[econ2]
    return None


def _is_carrier(st: dict) -> bool:
    return "Carrier" in str(st.get("type") or "")


def _populated_nearby(reference: str, radius: float, timeout: float) -> list[dict]:
    data = _get(_SPHERE, {"systemName": reference, "radius": radius,
                          "showInformation": 1, "showId": 1}, timeout)
    systems = [s for s in (data or []) if (s.get("information") or {}).get("population")]
    systems.sort(key=lambda s: s.get("distance") if s.get("distance") is not None else 1e9)
    return systems


def _stations_with_service(systems: list[dict], service: str, budget: int,
                           timeout: float) -> list[tuple[dict, dict]]:
    """Fetch stations for the nearest `budget` systems in parallel; return
    [(system, station), ...] for stations offering `service`, distance-sorted."""
    picks = systems[:budget]

    def fetch(sysrec):
        try:
            d = _get(_STATIONS, {"systemName": sysrec["name"]}, timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return sysrec, []
        return sysrec, (d.get("stations") or [])

    hits: list[tuple[dict, dict]] = []
    with ThreadPoolExecutor(max_workers=5) as pool:  # gentle on EDSM to avoid throttling
        for sysrec, stations in pool.map(fetch, picks):
            for st in stations:
                if _is_carrier(st):
                    continue
                if service in (st.get("otherServices") or []):
                    hits.append((sysrec, st))
    hits.sort(key=lambda pair: (pair[0].get("distance") or 1e9,
                                pair[1].get("distanceToArrival") or 1e9))
    return hits


def _row(sysrec: dict, st: dict) -> dict:
    info = sysrec.get("information") or {}
    return {"system": sysrec.get("name"), "station": st.get("name"),
            "distance_ly": sysrec.get("distance"), "distance_ls": st.get("distanceToArrival"),
            "economy": st.get("economy") or info.get("economy"),
            "allegiance": info.get("allegiance")}


def nearest_commodity_sources(reference: str, commodity: str, *, radius: float = 45,
                              system_budget: int = 18, station_budget: int = 28,
                              limit: int = 5, timeout: float = 15) -> list[dict]:
    """Stations that SELL `commodity` near `reference`, from EDSM market snapshots.

    EDSM has no 'who sells X' search, so we enumerate the nearest populated
    systems' stations, then read each station's market and keep the ones actually
    stocking the commodity. Bounded (nearest ~28 stations with a market) so it
    stays responsive. Fallback for when Spansh's commodity search is down —
    EDSM's market data is crowd-sourced and can be stale/incomplete.
    """
    ref = (reference or "").strip()
    if not ref:
        raise ValueError("no reference system")
    key = str(commodity).casefold()
    cache_key = ("commodity", ref.lower(), key)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(cache_key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

    systems = _populated_nearby(ref, radius, timeout)[:system_budget]

    def fetch_stations(sysrec):
        try:
            d = _get(_STATIONS, {"systemName": sysrec["name"]}, timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return sysrec, []
        return sysrec, (d.get("stations") or [])

    pairs: list[tuple[dict, dict]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for sysrec, stations in pool.map(fetch_stations, systems):
            for st in stations:
                if (st.get("haveMarket") and st.get("marketId")
                        and "Carrier" not in str(st.get("type") or "")):
                    pairs.append((sysrec, st))
    pairs.sort(key=lambda p: (p[0].get("distance") or 1e9, p[1].get("distanceToArrival") or 1e9))
    pairs = pairs[:station_budget]

    def fetch_market(pair):
        sysrec, st = pair
        try:
            d = _get(_MARKET, {"marketId": st["marketId"]}, timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        for c in d.get("commodities") or []:
            if str(c.get("name") or "").casefold() == key and (c.get("stock") or 0) > 0:
                return {"system": sysrec.get("name"), "station": st.get("name"),
                        "distance_ly": sysrec.get("distance"), "distance_ls": st.get("distanceToArrival"),
                        "buy_price": c.get("buyPrice"), "supply": c.get("stock"),
                        "economy": st.get("economy")}
        return None

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for r in pool.map(fetch_market, pairs):
            if r:
                out.append(r)
    out.sort(key=lambda r: (r.get("distance_ly") or 1e9, r.get("distance_ls") or 1e9))
    out = out[:limit]
    with _LOCK:
        _CACHE[cache_key] = (now, out)
    return out


def nearest_material_traders(reference: str, *, radius: float = 40, per_type: int = 3,
                             budget: int = 24, timeout: float = 18) -> dict[str, list]:
    """{raw:[...], manufactured:[...], encoded:[...]} nearest to `reference`."""
    ref = (reference or "").strip()
    if not ref:
        raise ValueError("no reference system")
    key = ("mat", ref.lower())
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    systems = _populated_nearby(ref, radius, timeout)
    hits = _stations_with_service(systems, "Material Trader", budget, timeout)
    groups: dict[str, list] = {"raw": [], "manufactured": [], "encoded": []}
    for sysrec, st in hits:
        kind = _mat_type(st.get("economy"), st.get("secondEconomy"))
        if kind and len(groups[kind]) < per_type:
            groups[kind].append(_row(sysrec, st))
    with _LOCK:
        _CACHE[key] = (time.time(), groups)
    return groups


def nearest_guardian_brokers(reference: str, *, radius: float = 60, limit: int = 8,
                             budget: int = 30, timeout: float = 20) -> list[dict]:
    """Nearest Guardian tech brokers (Technology Broker at a High-Tech-economy station).

    Guardian brokers are sparser than material traders, so pre-filter the sphere
    to High-Tech systems (where such stations live) before fetching stations —
    this reaches much farther per EDSM call than scanning every populated system.
    """
    ref = (reference or "").strip()
    if not ref:
        raise ValueError("no reference system")
    key = ("guardian", ref.lower())
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    systems = _populated_nearby(ref, radius, timeout)
    high_tech = [s for s in systems if (s.get("information") or {}).get("economy") == "High Tech"]
    hits = _stations_with_service(high_tech, "Technology Broker", budget, timeout)
    out: list[dict] = []
    for sysrec, st in hits:
        # Guardian broker = Technology Broker where primary OR secondary economy is High Tech
        if "High Tech" in (st.get("economy"), st.get("secondEconomy")):
            out.append(_row(sysrec, st))
        if len(out) >= limit:
            break
    with _LOCK:
        _CACHE[key] = (time.time(), out)
    return out
