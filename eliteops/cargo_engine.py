"""Cargo (trade) route engine for EliteOps — the cash-cow tool.

Wraps the proven RouteOps pieces (spansh_client.generate_trade + cargo.build_stops)
and drives a live, auto-ticking stop-by-stop checklist straight from the journal:
dock -> advances to that stop, MarketSell -> ticks Sell, MarketBuy -> ticks Buy and
copies the next system to the PC clipboard. All standard library.

Unlike the EDD panel (one stop at a time, no colour), the web UI can show the whole
route + every stop at once, colour-coded, on any device on the LAN.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

# vendored RouteOps modules live in ./vendor (bare imports inside them resolve there)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import cargo as cargo_mod  # noqa: E402
import spansh_client  # noqa: E402

try:
    import clipboard_service  # noqa: E402
except Exception:  # noqa: BLE001 - clipboard is best-effort
    clipboard_service = None  # type: ignore


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _commodity_key(entry: dict[str, Any]) -> str:
    """Squashed commodity key matching Spansh's names. MarketSell often lacks
    Type_Localised, and Type can be a '$xxx_name;' symbol."""
    for field in ("Type_Localised", "FriendlyType", "Type"):
        raw = entry.get(field)
        if not raw:
            continue
        text = str(raw)
        if text.startswith("$") and text.endswith(";"):
            text = text[1:-1]
            if text.endswith("_name"):
                text = text[:-5]
        key = "".join(ch for ch in text.lower() if ch.isalnum())
        if key:
            return key
    return ""


class CargoEngine:
    """Holds the current cargo route + live checklist state. Thread-safe."""

    def __init__(self, state) -> None:
        self.state = state
        self._lock = threading.Lock()
        self._route: dict[str, Any] | None = None
        self._stops: list[dict[str, Any]] = []
        self._stop = 0
        self._sold = False
        self._bought = False
        self._skipped: set[int] = set()
        self._status = "idle"       # idle | generating | ready | error
        self._error = ""
        self._params: dict[str, Any] = {}
        self._clipboard_note = ""
        state.add_journal_listener(self.on_journal)

    # --- generation ---------------------------------------------------------
    def generate(self, params: dict[str, Any]) -> None:
        """Kick off a Spansh trade-route search in a background thread."""
        with self._lock:
            self._status = "generating"
            self._error = ""
            self._params = dict(params)
        threading.Thread(target=self._run_generate, args=(params,),
                         name="eliteops-cargo-gen", daemon=True).start()

    def _run_generate(self, params: dict[str, Any]) -> None:
        try:
            result = spansh_client.generate_trade(
                system=params["system"],
                station=params.get("station", ""),
                cargo=int(params.get("cargo") or 720),
                max_hops=int(params.get("max_hops") or 5),
                large_pad_only=bool(params.get("large_pad_only", True)),
                max_hop_distance=float(params.get("max_hop_distance") or 50.0),
                allow_planetary=bool(params.get("allow_planetary", True)),
                loop=bool(params.get("loop", True)),
                max_arrival_distance=params.get("max_arrival_distance") or None,
            )
        except Exception as exc:  # noqa: BLE001 - surface as UI error
            with self._lock:
                self._status = "error"
                self._error = str(exc)
            return
        with self._lock:
            self._route = result
            self._stops = cargo_mod.build_stops(result)
            self._stop = 0
            self._sold = False
            self._bought = False
            self._skipped = set()
            self._status = "ready"
            self._error = ""
            self._clipboard_note = ""

    def load(self, route: dict[str, Any]) -> None:
        with self._lock:
            self._route = route
            self._stops = cargo_mod.build_stops(route)
            self._stop = 0
            self._sold = self._bought = False
            self._skipped = set()
            self._status = "ready"
            self._error = ""

    # --- live tracking (ported from RouteOps _cargo_track) ------------------
    def on_journal(self, entry: dict[str, Any]) -> None:
        with self._lock:
            stops = self._stops
            if not stops:
                return
            event = (entry.get("event") or entry.get("EventTypeID")
                     or entry.get("EventTypeStr") or entry.get("Event"))
            cur = min(self._stop, len(stops) - 1)
            s = stops[cur]
            # Only DOCKING changes which stop you're on -- selling/buying merely tick
            # the current stop. This stops an off-route sale (e.g. mined Cobalt) from
            # yanking the checklist to a far hop that happens to trade that commodity.
            if event == "Docked":
                idx = self._find_stop(station=_norm(entry.get("StationName")))
                if idx is None:
                    idx = self._find_stop(system=_norm(entry.get("StarSystem")))
                if idx is not None and idx != cur:
                    self._goto(idx)
            elif event == "MarketSell":
                commodity = _commodity_key(entry)
                if commodity and commodity in s["sell"]:
                    self._sold = True
            elif event == "MarketBuy":
                commodity = _commodity_key(entry)
                if commodity and commodity in s["buy"]:
                    self._bought = True
                    fly = s.get("fly_system")
                    if fly:
                        self._copy(fly)

    def _find_stop(self, *, station: str = "", system: str = "",
                   sell: str = "", buy: str = "") -> int | None:
        # Strongest signal first: an exact STATION is where you physically docked,
        # so it wins over a mere system match at some earlier stop.
        stops = self._stops
        order = list(range(self._stop, len(stops))) + list(range(0, self._stop))
        if station:
            for i in order:
                if _norm(stops[i].get("station")) == station:
                    return i
        if system:
            for i in order:
                if _norm(stops[i].get("system")) == system:
                    return i
        if sell:
            for i in order:
                if sell in stops[i]["sell"]:
                    return i
        if buy:
            for i in order:
                if buy in stops[i]["buy"]:
                    return i
        return None

    def _goto(self, index: int) -> None:
        self._stop = index
        self._sold = False
        self._bought = False

    def _copy(self, text: str) -> bool:
        if not text or clipboard_service is None:
            return False
        try:
            result = clipboard_service.copy_text(str(text))
            ok = bool(getattr(result, "success", False))
            self._clipboard_note = f"Copied {text} to clipboard" if ok else ""
            return ok
        except Exception:  # noqa: BLE001
            return False

    # --- user actions from the web UI --------------------------------------
    def goto_stop(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._stops):
                self._goto(index)

    def toggle_skip(self, hop: int) -> None:
        with self._lock:
            if hop in self._skipped:
                self._skipped.discard(hop)
            else:
                self._skipped.add(hop)

    def copy_system(self, system: str) -> bool:
        with self._lock:
            return self._copy(system)

    def reset(self) -> None:
        with self._lock:
            self._route = None
            self._stops = []
            self._stop = 0
            self._sold = self._bought = False
            self._skipped = set()
            self._status = "idle"
            self._error = ""

    # --- serialisation for the UI ------------------------------------------
    def _route_view(self) -> dict[str, Any]:
        route = self._route or {}
        hops = route.get("hops") or []
        out_hops = []
        for i, hop in enumerate(hops):
            src = hop.get("source") or {}
            dst = hop.get("destination") or {}
            coms = []
            for c in (hop.get("commodities") or []):
                coms.append({
                    "name": c.get("name"),
                    "amount": c.get("amount"),
                    "buy": (c.get("source_commodity") or {}).get("buy_price"),
                    "sell": (c.get("destination_commodity") or {}).get("sell_price"),
                    "profit": int(c.get("total_profit") or 0),
                })
            out_hops.append({
                "n": i + 1,
                "commodities": coms,
                "from_system": src.get("system"), "from_station": src.get("station"),
                "from_ls": src.get("distance_to_arrival"),
                "to_system": dst.get("system"), "to_station": dst.get("station"),
                "to_ls": dst.get("distance_to_arrival"),
                "distance": hop.get("distance"),
                "tons": cargo_mod.hop_tons(hop),
                "profit": int(hop.get("total_profit") or 0),
                "skipped": i in self._skipped,
            })
        approach = route.get("approach")
        return {
            "from_system": route.get("from_system"),
            "start_system": route.get("start_system"),
            "start_station": route.get("start_station"),
            "approach": ({
                "system": approach.get("system"), "station": approach.get("station"),
                "distance_ly": approach.get("distance_ly"), "distance_ls": approach.get("distance_ls"),
            } if approach else None),
            "total_profit": cargo_mod.total_profit(route),
            "total_distance": cargo_mod.total_distance(route),
            "hops": out_hops,
        }

    def _stops_view(self) -> list[dict[str, Any]]:
        cur = min(self._stop, len(self._stops) - 1) if self._stops else 0
        out = []
        for i, s in enumerate(self._stops):
            out.append({
                "n": i + 1,
                "system": s.get("system"), "station": s.get("station"),
                "sell_label": s.get("sell_label"), "has_sell": bool(s.get("sell")),
                "buy_label": s.get("buy_label"), "has_buy": bool(s.get("buy")),
                "fly_system": s.get("fly_system"), "fly_station": s.get("fly_station"),
                "hop": s.get("hop"),
                "current": i == cur,
                "sold": (i == cur) and self._sold,
                "bought": (i == cur) and self._bought,
                "skipped": s.get("hop") in self._skipped,
            })
        return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "error": self._error,
                "params": self._params,
                "clipboard_note": self._clipboard_note,
                "current_stop": min(self._stop, len(self._stops) - 1) if self._stops else 0,
                "sold": self._sold,
                "bought": self._bought,
                "route": self._route_view() if self._route else None,
                "stops": self._stops_view(),
            }

    def prefill(self) -> dict[str, Any]:
        """Sensible defaults from live state so the form starts pre-filled."""
        snap = self.state.snapshot()
        jr = snap.get("jump_range")
        return {
            "system": snap.get("system") or "",
            "station": snap.get("station") or "",
            "cargo": snap.get("cargo_capacity") or 720,
            "jump_range": round(jr, 2) if isinstance(jr, (int, float)) else None,
        }

    # --- persistence (survive a server restart) -----------------------------
    def persist(self) -> dict[str, Any]:
        with self._lock:
            return {"route": self._route, "stop": self._stop, "sold": self._sold,
                    "bought": self._bought, "skipped": list(self._skipped),
                    "status": self._status, "params": self._params}

    def restore(self, data: dict[str, Any] | None) -> None:
        if not data or not data.get("route"):
            return
        with self._lock:
            self._route = data["route"]
            self._stops = cargo_mod.build_stops(self._route)
            self._stop = int(data.get("stop") or 0)
            self._sold = bool(data.get("sold"))
            self._bought = bool(data.get("bought"))
            self._skipped = set(data.get("skipped") or [])
            self._status = data.get("status") or "ready"
            self._params = data.get("params") or {}
