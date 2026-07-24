"""Inara 'nearest stations' scraper — the accurate source for services that
Spansh's data doesn't carry reliably (Material Traders, Technology Brokers,
Interstellar Factors).

Spansh knows where stations are but its per-station `services` list is empty for
most stations, so filtering by "Material Trader" there returns economy-matched
guesses that usually have no trader. Inara crowd-sources the actual service list
and even classifies the material-trader TYPE (Raw / Manufactured / Encoded) and
the tech-broker type (Guardian / Human) for us.

This hits the public GET form at inara.cz/elite/nearest-stations/ and parses the
result table. Standard library only; best-effort and cached briefly.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EliteOps/1.0"
_BASE = "https://inara.cz/elite/nearest-stations/"

# Inara service codes (pa1[] on the nearest-stations form)
SERVICE = {
    "material_raw": "25-10",
    "material_manufactured": "25-11",
    "material_encoded": "25-12",
    "material_any": "25",
    "broker_guardian": "26-30",
    "broker_human": "26-10",
    "interstellar_factors": "18",
}

# result column order (Inara 'brief' layout)
_COLS = ["trader_type", "station", "system", "economy", "government",
         "allegiance", "station_dist_ls", "distance_ly"]

_CACHE: dict[tuple, tuple[float, list]] = {}
_CACHE_TTL = 300.0  # 5 min — trader locations don't churn fast
_LOCK = threading.Lock()


def _strip(html: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", unescape(html)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", txt).strip()


def _name(html: str) -> str:
    """Clean a station/system name cell. Elite names are ASCII, so cut at the
    first non-ASCII char — that drops Inara's trailing icon-font glyphs (pad
    size, station type) which otherwise render as junk."""
    txt = _strip(html)
    return re.sub(r"[^\x00-\x7F].*$", "", txt).strip()


def _parse(html: str) -> list[dict]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    out: list[dict] = []
    for r in rows:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        if len(tds) < 6:
            continue  # header / spacer / not a data row
        cells = [_strip(td) for td in tds]
        # the two distance cells carry a clean numeric in data-order=""
        orders = re.findall(r'data-order="([-0-9.]+)"', r)
        row = {c: (cells[i] if i < len(cells) else "") for i, c in enumerate(_COLS)}
        # station (col 1) and system (col 2) cells carry trailing icon glyphs — clean them
        if len(tds) > 1:
            row["station"] = _name(tds[1])
        if len(tds) > 2:
            row["system"] = _name(tds[2])
        # overwrite the distance fields with numeric values when available
        if len(orders) >= 2:
            try:
                row["station_dist_ls"] = float(orders[-2])
            except ValueError:
                pass
            try:
                row["distance_ly"] = float(orders[-1])
            except ValueError:
                pass
        sh = re.search(r"/elite/station/(\d+)/", r)
        yh = re.search(r"/elite/starsystem/(\d+)/", r)
        row["inara_station_id"] = sh.group(1) if sh else None
        row["inara_system_id"] = yh.group(1) if yh else None
        if row["station"] and row["system"]:
            out.append(row)
    return out


def nearest_stations(reference_system: str, service: str, *, limit: int = 20,
                     timeout: float = 20.0) -> list[dict]:
    """Nearest stations offering `service` (a key in SERVICE) to `reference_system`.

    Returns rows: trader_type, station, system, economy, government, allegiance,
    station_dist_ls, distance_ly (+ inara ids). Sorted nearest-first by Inara.
    """
    code = SERVICE.get(service)
    if not code:
        raise ValueError(f"unknown service '{service}'")
    ref = str(reference_system or "").strip()
    if not ref:
        raise ValueError("no reference system")

    key = (ref.lower(), code)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1][:limit]

    qs = urllib.parse.urlencode([("formbrief", "1"), ("ps1", ref), ("pa1[]", code)])
    req = urllib.request.Request(_BASE + "?" + qs, headers={"User-Agent": _UA})

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", "replace")
            rows = _parse(html)
            with _LOCK:
                _CACHE[key] = (time.time(), rows)
            return rows[:limit]
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))  # brief backoff; Inara throttles bursts
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(1.0)
                continue
            break

    # transient failure — serve a (possibly stale) cache entry if we have one
    with _LOCK:
        stale = _CACHE.get(key)
    if stale:
        return stale[1][:limit]
    raise RuntimeError(f"Inara request failed: {last_err}")
