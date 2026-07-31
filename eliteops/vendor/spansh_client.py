"""Spansh route generation client (standard-library only).

RouteOps generates routes directly from the Spansh plotters
(https://spansh.co.uk) instead of requiring a manual export/import. Each plotter
is an asynchronous job: submit parameters, receive a job id, then poll a results
endpoint until it reports ``ok``. The completed result is flattened and handed to
the plugin's existing importers, so a generated route follows the exact same
compile/load/library path as a file the user opened.

Only the Python standard library is used (urllib), matching the plugin's existing
dependency footprint (pyzmq + stdlib). Network work is blocking, so callers should
run generation on a worker thread and load the resulting route on the main thread
(see routeops_kernel_app background pump).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

SPANSH_API = "https://spansh.co.uk/api"
USER_AGENT = "RouteOps-EDDiscovery-Plugin"

ProgressCallback = Callable[[str], None]


class SpanshError(Exception):
    """A recoverable Spansh generation failure with a user-facing message.

    ``status`` is the HTTP status when Spansh rejected the request (e.g. 400 for an
    unknown reference system), or None for network/other failures.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _http_error(exc: "urllib.error.HTTPError") -> SpanshError:
    message = None
    try:
        body = json.load(exc)
        if isinstance(body, dict):
            message = body.get("error")
    except Exception:
        message = None
    return SpanshError(str(message) if message else f"Spansh rejected the request (HTTP {exc.code}).", status=exc.code)


def _post(path: str, params: dict[str, Any], timeout: float = 30.0) -> Any:
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(SPANSH_API + path, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SpanshError(f"Could not reach Spansh: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpanshError("Spansh returned an unreadable response.") from exc


def _get(path: str, timeout: float = 30.0) -> Any:
    request = urllib.request.Request(SPANSH_API + path, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SpanshError(f"Could not reach Spansh: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpanshError("Spansh returned an unreadable response.") from exc


def _jpost(path: str, body: dict[str, Any], timeout: float = 30.0) -> Any:
    request = urllib.request.Request(
        SPANSH_API + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SpanshError(f"Could not reach Spansh: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpanshError("Spansh returned an unreadable response.") from exc


_ACQUIRE_STATION_TYPES = [
    "Asteroid Base", "Coriolis Starport", "Mega Ship", "Ocellus Starport",
    "Orbis Starport", "Outpost", "Planetary Outpost", "Planetary Port",
    "Settlement", "Dockable Planet Station", "Dodec Starport",
]


def _acquire_common(radius: float, large_pad: bool, include_carriers: bool) -> dict[str, Any]:
    filters: dict[str, Any] = {"distance": {"min": "0", "max": f"{radius:.3f}"}}
    if large_pad:
        filters["has_large_pad"] = {"value": True}
    if not include_carriers:
        filters["type"] = {"value": _ACQUIRE_STATION_TYPES}
    return filters


def _acquire_search(reference_system: str, filters: dict[str, Any], size: int = 15,
                    timeout: float = 30.0) -> list[dict[str, Any]]:
    body = {"filters": filters, "sort": [{"distance": {"direction": "asc"}}],
            "reference_system": str(reference_system).strip(), "size": size}
    data = _jpost("/stations/search", body, timeout=timeout)
    out = []
    for s in (data.get("results") if isinstance(data, dict) else []) or []:
        stype = str(s.get("type") or "")
        name = str(s.get("name") or "")
        if "Carrier" in stype or name.endswith("Carrier"):
            continue  # never route a build shop to a fleet carrier
        out.append({
            "system": s.get("system_name"), "station": name,
            "distance_ly": s.get("distance"), "distance_ls": s.get("distance_to_arrival"),
            "type": stype, "large_pad": s.get("has_large_pad"), "updated_at": s.get("updated_at"),
        })
    return out


def find_module_stations(reference_system: str, name: str, module_class: Any = None,
                         rating: Any = None, *, radius: float = 100.0, large_pad: bool = True,
                         include_carriers: bool = False, size: int = 15) -> list[dict[str, Any]]:
    """Stations selling a given outfitting module, nearest first."""
    mod: dict[str, Any] = {"name": [name]}
    if module_class not in (None, "", 0):
        mod["class"] = [str(module_class)]
    if rating:
        mod["rating"] = [str(rating).strip().upper()]
    filters = _acquire_common(radius, large_pad, include_carriers)
    filters["modules"] = mod
    return _acquire_search(reference_system, filters, size)


def find_ship_stations(reference_system: str, ship_name: str, *, radius: float = 120.0,
                       large_pad: bool = True, include_carriers: bool = False,
                       size: int = 15) -> list[dict[str, Any]]:
    """Stations with a shipyard selling a given ship, nearest first."""
    filters = _acquire_common(radius, large_pad, include_carriers)
    filters["ships"] = {"value": [ship_name]}
    return _acquire_search(reference_system, filters, size)


def find_nearest_trade_station(
    system: str, *, large_pad_only: bool = True, allow_planetary: bool = True, timeout: float = 30.0
) -> dict[str, Any] | None:
    """Find the nearest real market station to ``system`` (for a cargo-route start).

    Skips colonisation ships, fleet carriers and stations with no live market data.
    """
    # NB: the boolean field is "has_market" - the "market" filter expects a commodity
    # list and makes Spansh's search 500 ("Could not perform search") intermittently.
    filters: dict[str, Any] = {"has_market": {"value": True}}
    if large_pad_only:
        filters["has_large_pad"] = {"value": True}
    if not allow_planetary:
        filters["is_planetary"] = {"value": False}
    body = {
        "filters": filters,
        "reference_system": str(system).strip(),
        "sort": [{"distance": {"direction": "asc"}}],
        "size": 30,
    }
    data = None
    for attempt in range(3):
        try:
            data = _jpost("/stations/search", body, timeout=timeout)
            break
        except SpanshError as exc:
            status = getattr(exc, "status", None)
            if status == 400:  # bad request == unknown reference system
                raise SpanshError(
                    f"Spansh has no data for '{system}' - the system may be unexplored. "
                    "Enter a start system nearer populated space."
                ) from exc
            if status in (500, 502, 503) and attempt < 2:
                time.sleep(1.0)  # transient server error - retry
                continue
            raise
    for station in data.get("results", []) if isinstance(data, dict) else []:
        name = str(station.get("name") or "")
        stype = str(station.get("type") or "")
        if not station.get("has_market") or not station.get("updated_at"):
            continue  # no live market data (construction ships, unstocked carriers)
        if name.startswith("$") or "Colonisation Ship" in name or "Carrier" in stype or "Construction" in name:
            continue
        if not allow_planetary and station.get("is_planetary"):
            continue
        return {
            "system": station.get("system_name"),
            "station": name,
            "distance_ly": station.get("distance"),
            "distance_ls": station.get("distance_to_arrival"),
            "type": stype,
        }
    return None


def _await_job(
    submit_response: Any,
    *,
    poll_timeout: float,
    interval: float = 2.0,
    on_progress: ProgressCallback | None = None,
) -> Any:
    if not isinstance(submit_response, dict) or not submit_response.get("job"):
        error = submit_response.get("error") if isinstance(submit_response, dict) else None
        raise SpanshError(str(error or "Spansh did not accept the request."))
    job = submit_response["job"]
    deadline = time.monotonic() + poll_timeout
    polls = 0
    while time.monotonic() < deadline:
        time.sleep(interval)
        result = _get(f"/results/{job}")
        status = str(result.get("status", "")) if isinstance(result, dict) else ""
        if status == "ok":
            return result.get("result")
        if status in {"failed", "error"}:
            raise SpanshError(str(result.get("error") or "Spansh route job failed."))
        polls += 1
        if on_progress:
            on_progress(f"Spansh working ({status or 'queued'})... {polls * interval:.0f}s")
    raise SpanshError("Timed out waiting for the Spansh route.")


# --------------------------------------------------------------------------- #
# Exobiology (Expressway to Exomastery)
# --------------------------------------------------------------------------- #

def _flatten_exobiology(result: Any) -> list[dict[str, Any]]:
    """Flatten Spansh's nested systems->bodies->landmarks into importer rows.

    A Spansh "landmark" is one biological find: ``subtype`` = full species,
    ``type`` = genus, ``value`` = credits. spansh_exobiology_importer already
    understands these row keys.
    """
    rows: list[dict[str, Any]] = []
    for system in result or []:
        if not isinstance(system, dict):
            continue
        system_name = system.get("name")
        system_address = system.get("id64")
        for body in system.get("bodies") or []:
            if not isinstance(body, dict):
                continue
            landmarks = body.get("landmarks") or []
            common = {
                "system": system_name,
                "system_address": system_address,
                "body": body.get("name"),
                "body_id": body.get("id64"),
                "distance": body.get("distance_to_arrival"),
                "biological_signals": len(landmarks),
            }
            for landmark in landmarks:
                if not isinstance(landmark, dict):
                    continue
                rows.append(
                    {
                        **common,
                        "species": landmark.get("subtype"),
                        "genus": landmark.get("type"),
                        "value": landmark.get("value"),
                    }
                )
    return rows


def exobiology_route(
    *,
    from_system: str,
    jump_range: float,
    radius: float,
    min_value: int = 0,
    max_results: int = 30,
    loop: bool = False,
    use_mapping_value: bool = True,
    poll_timeout: float = 180.0,
    on_progress: "ProgressCallback | None" = None,
) -> dict[str, Any]:
    """Generate an exobiology route and return a CLEAN nested structure (no RouteOps
    importer needed): {from_system, systems:[{name,id64,jumps,total_value,bodies:[...]}],
    total_value}. Each body carries its landmark species with per-species value."""
    if not str(from_system).strip():
        raise SpanshError("A start system is required.")
    params = {
        "from": str(from_system).strip(), "range": jump_range, "radius": radius,
        "max_results": max_results, "min_value": min_value,
        "loop": 1 if loop else 0, "use_mapping_value": 1 if use_mapping_value else 0,
    }
    result = _await_job(_post("/exobiology/route", params),
                        poll_timeout=poll_timeout, on_progress=on_progress)
    systems = []
    grand_total = 0
    for s in result or []:
        if not isinstance(s, dict):
            continue
        bodies = []
        sys_total = 0
        for b in s.get("bodies") or []:
            if not isinstance(b, dict):
                continue
            species = [{"genus": lm.get("type"), "species": lm.get("subtype"),
                        "value": lm.get("value"), "count": lm.get("count")}
                       for lm in (b.get("landmarks") or []) if isinstance(lm, dict)]
            bval = int(b.get("landmark_value") or 0)
            sys_total += bval
            bodies.append({
                "name": b.get("name"), "distance_ls": b.get("distance_to_arrival"),
                "subtype": b.get("subtype"), "landmark_value": bval,
                "scan_value": b.get("estimated_scan_value"),
                "mapping_value": b.get("estimated_mapping_value"),
                "species": sorted(species, key=lambda x: -(x["value"] or 0)),
            })
        if not bodies:
            continue  # skip empty (e.g. the start system)
        grand_total += sys_total
        systems.append({"name": s.get("name"), "id64": s.get("id64"),
                        "jumps": s.get("jumps"), "total_value": sys_total,
                        "bodies": sorted(bodies, key=lambda x: -x["landmark_value"])})
    if not systems:
        raise SpanshError("Spansh found no exobiology bodies for those parameters.")
    return {"from_system": str(from_system).strip(), "systems": systems,
            "total_value": grand_total}


def _post_multi(path: str, pairs: list, timeout: float = 30.0) -> Any:
    """Like _post but takes a LIST OF TUPLES, so a parameter can repeat (the tourist
    plotter wants one `destination` per stop)."""
    data = urllib.parse.urlencode(pairs).encode("utf-8")
    request = urllib.request.Request(SPANSH_API + path, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SpanshError(f"Could not reach Spansh: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpanshError("Spansh returned an unreadable response.") from exc


def _waypoints_from_jumps(result: Any) -> list[dict[str, Any]]:
    """Normalise a `system_jumps` payload (neutron + tourist share it)."""
    out = []
    for w in (result.get("system_jumps") if isinstance(result, dict) else []) or []:
        if not isinstance(w, dict):
            continue
        out.append({
            "system": w.get("system"), "id64": w.get("id64"),
            "jumps": w.get("jumps"),
            "neutron": bool(w.get("neutron_star")),
            "distance_jumped": w.get("distance_jumped"),
            "distance_left": w.get("distance_left"),
            "x": w.get("x"), "y": w.get("y"), "z": w.get("z"),
        })
    return out


def neutron_route(*, from_system: str, to_system: str, jump_range: float,
                  efficiency: int = 60, poll_timeout: float = 180.0,
                  on_progress: "ProgressCallback | None" = None) -> dict[str, Any]:
    """Neutron-highway plot A->B. NOTE this endpoint takes `from`/`to`."""
    if not str(from_system).strip() or not str(to_system).strip():
        raise SpanshError("Both a start and a destination system are required.")
    result = _await_job(
        _post("/route", {"from": str(from_system).strip(), "to": str(to_system).strip(),
                         "range": jump_range, "efficiency": efficiency}),
        poll_timeout=poll_timeout, on_progress=on_progress)
    waypoints = _waypoints_from_jumps(result)
    if not waypoints:
        raise SpanshError("Spansh could not plot a route between those systems.")
    return {"kind": "neutron",
            "from_system": (result or {}).get("source_system") or from_system,
            "to_system": (result or {}).get("destination_system") or to_system,
            "distance": (result or {}).get("distance"),
            "total_jumps": (result or {}).get("total_jumps"),
            "waypoints": waypoints}


def tourist_route(*, source: str, destinations: list, jump_range: float,
                  poll_timeout: float = 180.0,
                  on_progress: "ProgressCallback | None" = None) -> dict[str, Any]:
    """Multi-stop plot that ORDERS the stops for you. NOTE this endpoint takes
    `source` + a repeated `destination` (not from/to)."""
    stops = [str(d).strip() for d in (destinations or []) if str(d).strip()]
    if not str(source).strip() or not stops:
        raise SpanshError("A start system and at least one destination are required.")
    pairs = [("source", str(source).strip()), ("range", str(jump_range))]
    pairs += [("destination", d) for d in stops]
    result = _await_job(_post_multi("/tourist/route", pairs),
                        poll_timeout=poll_timeout, on_progress=on_progress)
    waypoints = _waypoints_from_jumps(result)
    if not waypoints:
        raise SpanshError("Spansh could not plot a route through those systems.")
    return {"kind": "tourist",
            "from_system": (result or {}).get("source_system") or source,
            "destinations": stops, "waypoints": waypoints}


def riches_route(*, from_system: str, jump_range: float, radius: float = 100.0,
                 max_results: int = 25, min_value: int = 500_000,
                 use_mapping_value: bool = True, loop: bool = False,
                 poll_timeout: float = 180.0,
                 on_progress: "ProgressCallback | None" = None) -> dict[str, Any]:
    """Road to Riches — a circuit of high-value systems with the bodies worth scanning."""
    if not str(from_system).strip():
        raise SpanshError("A start system is required.")
    result = _await_job(
        _post("/riches/route", {"from": str(from_system).strip(), "range": jump_range,
                                "radius": radius, "max_results": max_results,
                                "min_value": min_value,
                                "use_mapping_value": 1 if use_mapping_value else 0,
                                "loop": 1 if loop else 0}),
        poll_timeout=poll_timeout, on_progress=on_progress)
    systems, grand_total = [], 0
    for s in result or []:
        if not isinstance(s, dict):
            continue
        bodies = []
        for b in s.get("bodies") or []:
            if not isinstance(b, dict):
                continue
            val = int(b.get("estimated_mapping_value") or b.get("estimated_scan_value") or 0)
            bodies.append({"name": b.get("name"), "subtype": b.get("subtype"),
                           "distance_ls": b.get("distance_to_arrival"),
                           "scan_value": b.get("estimated_scan_value"),
                           "mapping_value": b.get("estimated_mapping_value"),
                           "value": val, "landmark_value": b.get("landmark_value")})
        if not bodies:
            continue  # skip the start system / empties
        sys_total = sum(b["value"] for b in bodies)
        grand_total += sys_total
        systems.append({"name": s.get("name"), "id64": s.get("id64"),
                        "jumps": s.get("jumps"), "total_value": sys_total,
                        "bodies": sorted(bodies, key=lambda x: -(x["value"] or 0))})
    if not systems:
        raise SpanshError("Spansh found no high-value systems for those parameters.")
    return {"kind": "riches", "from_system": str(from_system).strip(),
            "systems": systems, "total_value": grand_total}


def find_commodity_sales(reference_system: str, commodity: str, amount: int, *,
                         large_pad_only: bool = True, limit: int = 6,
                         timeout: float = 25.0) -> list[dict[str, Any]]:
    """Where to SELL ``commodity`` — best sell price first. Uses Spansh's
    /commodity/sell/{system}/{commodity}/{amount} endpoint (mirror of the buy finder)."""
    path = ("/commodity/sell/" + urllib.parse.quote(str(reference_system)) + "/"
            + urllib.parse.quote(str(commodity)) + "/" + str(int(max(1, amount))))
    data = _get(path, timeout=timeout)
    stations = data.get("results") if isinstance(data, dict) else data
    if not isinstance(stations, list):
        return []
    key = str(commodity).casefold()
    out = []
    for st in stations:
        if not isinstance(st, dict):
            continue
        if large_pad_only and not st.get("has_large_pad"):
            continue
        if "Carrier" in str(st.get("type") or ""):
            continue
        sell_price = demand = None
        for entry in st.get("market") or []:
            if str(entry.get("commodity", "")).casefold() == key:
                sell_price = entry.get("sell_price")
                demand = entry.get("demand")
                break
        if not sell_price:
            continue
        out.append({"station": st.get("name"), "system": st.get("system_name"),
                    "distance_ly": st.get("distance"), "distance_ls": st.get("distance_to_arrival"),
                    "sell_price": sell_price, "demand": demand,
                    "large_pad": st.get("has_large_pad"),
                    "market_updated_at": st.get("market_updated_at")})
    out.sort(key=lambda x: -(x["sell_price"] or 0))
    return out[:limit]


def generate_exobiology(
    *,
    from_system: str,
    jump_range: float,
    radius: float,
    min_value: int = 0,
    max_results: int = 50,
    loop: bool = False,
    use_mapping_value: bool = True,
    name: str | None = None,
    poll_timeout: float = 180.0,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Generate an exobiology route and return a RouteOps route dict."""
    if not str(from_system).strip():
        raise SpanshError("A start system is required.")
    params = {
        "from": str(from_system).strip(),
        "range": jump_range,
        "radius": radius,
        "max_results": max_results,
        "min_value": min_value,
        "loop": 1 if loop else 0,
        "use_mapping_value": 1 if use_mapping_value else 0,
    }
    result = _await_job(
        _post("/exobiology/route", params), poll_timeout=poll_timeout, on_progress=on_progress
    )
    rows = _flatten_exobiology(result)
    if not rows:
        raise SpanshError("Spansh found no exobiology bodies for those parameters.")
    from spansh_exobiology_importer import rows_to_routeops_v3

    route = rows_to_routeops_v3(
        rows, name or f"Spansh Exobiology from {from_system}", "spansh-exobiology-api"
    )
    if not route:
        raise SpanshError("Could not convert the Spansh result into a RouteOps route.")
    return route


# --------------------------------------------------------------------------- #
# Commodity sourcing ("where do I buy X near here") for colonisation supply
# --------------------------------------------------------------------------- #

def find_commodity_sources(
    reference_system: str,
    commodity: str,
    amount: int,
    *,
    large_pad_only: bool = True,
    limit: int = 5,
    timeout: float = 45.0,
) -> list[dict[str, Any]]:
    """Return stations that SELL ``commodity``, NEAREST to ``reference_system`` first.

    Uses ``/api/stations/search`` with a ``market_buy`` supply filter. (The old
    GET ``/commodity/buy/{system}/{commodity}/{amount}`` endpoint was removed by
    Spansh and now hangs on every request — including common goods — which is why
    the "find market" button silently never returned.) Station search IS
    distance-sortable, but we still re-sort defensively. Slower (~25-35s) but it
    actually works. Each source carries buy price, supply, pad and distances.
    """
    key = str(commodity).casefold()
    body = {
        "filters": {
            "market_buy": [{"name": str(commodity), "supply": {"value": ["1", "1000000000"]}}],
            "type": {"value": _ACQUIRE_STATION_TYPES},
        },
        "sort": [{"distance": {"direction": "asc"}}],
        "reference_system": str(reference_system),
        "size": 50,
    }
    data = _jpost("/stations/search", body, timeout=timeout)
    stations = data.get("results") if isinstance(data, dict) else data
    if not isinstance(stations, list):
        return []
    sources: list[dict[str, Any]] = []
    for station in stations:
        if not isinstance(station, dict):
            continue
        if "Carrier" in str(station.get("type") or ""):
            continue
        if large_pad_only and not station.get("has_large_pad"):
            continue
        buy_price = supply = None
        for entry in station.get("market_buy") or station.get("market") or []:
            if str(entry.get("commodity") or entry.get("name") or "").casefold() == key:
                buy_price = entry.get("buy_price") or entry.get("price")
                supply = entry.get("supply")
                break
        sources.append(
            {
                "station": station.get("name"),
                "system": station.get("system_name"),
                "distance_ly": station.get("distance"),
                "distance_ls": station.get("distance_to_arrival"),
                "large_pads": station.get("has_large_pad"),
                "is_planetary": station.get("is_planetary"),
                "buy_price": buy_price,
                "supply": supply,
                "market_updated_at": station.get("market_updated_at"),
            }
        )
    sources.sort(
        key=lambda s: s["distance_ly"] if isinstance(s.get("distance_ly"), (int, float)) else float("inf")
    )
    return sources[:limit]


# --------------------------------------------------------------------------- #
# Trade / cargo routes (Spansh trade planner)
# --------------------------------------------------------------------------- #

def _trade_route(params: dict[str, Any], poll_timeout: float, on_progress: ProgressCallback | None) -> list[dict[str, Any]]:
    result = _await_job(_post("/trade/route", params), poll_timeout=poll_timeout, on_progress=on_progress)
    if not isinstance(result, list) or not result:
        raise SpanshError("Spansh found no profitable cargo route for those parameters.")
    return result


def generate_trade(
    *,
    system: str,
    station: str = "",
    cargo: int = 720,
    max_hops: int = 5,
    large_pad_only: bool = True,
    max_hop_distance: float = 50.0,
    max_system_distance: float = 25.0,
    starting_capital: int = 100_000_000,
    allow_planetary: bool = True,
    unique: bool = True,
    loop: bool = True,
    max_arrival_distance: int | None = None,
    poll_timeout: float = 180.0,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Generate a profit-optimised cargo route starting from wherever you are.

    Give a start ``system``; ``station`` is optional. If it is blank (or the given
    station has no market Spansh can trade from), RouteOps finds the nearest real
    market station and starts there — returning an ``approach`` describing the hop
    to that first station. Returns ``{hops, start_system, start_station, approach,
    from_system}``.
    """
    system = str(system).strip()
    station = str(station or "").strip()
    if not system:
        raise SpanshError("A start system is required for a cargo route.")
    params = {
        "max_hops": max(1, min(int(max_hops), 10)),  # Spansh caps trade routes at 10 hops
        "max_hop_distance": max_hop_distance,
        "max_system_distance": max_system_distance,
        "starting_capital": int(starting_capital),
        "max_cargo": int(cargo),
        "max_price_age": 1_000_000_000,
        "requires_large_pad": 1 if large_pad_only else 0,
        "allow_planetary": 1 if allow_planetary else 0,
        "unique": 1 if unique else 0,
        "loop": 1 if loop else 0,
        "permit": 0,
    }
    if max_arrival_distance:
        params["max_distance"] = int(max_arrival_distance)  # max station distance from arrival (Ls)
    approach: dict[str, Any] | None = None

    def with_found() -> list[dict[str, Any]]:
        nonlocal approach, station
        if on_progress:
            on_progress("Finding nearest trading station...")
        found = find_nearest_trade_station(system, large_pad_only=large_pad_only, allow_planetary=allow_planetary)
        if not found:
            raise SpanshError(f"No large-pad trading station with a market found near {system}.")
        approach = found
        resolved_station = str(found["station"])
        route = _trade_route(
            {**params, "system": str(found["system"]), "station": resolved_station},
            poll_timeout, on_progress,
        )
        station = resolved_station
        return route

    if station:
        try:
            hops = _trade_route({**params, "system": system, "station": station}, poll_timeout, on_progress)
            start_system = system
        except SpanshError:
            # Given station isn't a market Spansh can trade from (e.g. a construction
            # ship) -> fall back to the nearest real market near the system.
            hops = with_found()
            start_system = str(approach["system"]) if approach else system
    else:
        hops = with_found()
        start_system = str(approach["system"]) if approach else system

    return {
        "hops": hops,
        "from_system": system,
        "start_system": start_system,
        "start_station": station,
        "approach": approach,
    }
