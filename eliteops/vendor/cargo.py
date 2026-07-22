"""Cargo (trade) route rendering for the CARGOGRID view.

Turns the hop list from spansh_client.generate_trade into grid rows:
Hop | Commodity | Buy at | Sell at | Ly | Profit | Cumulative, with human-readable
credit values and a full buy/sell/jump tooltip per hop. Standard library only.
"""

from __future__ import annotations

from typing import Any


def _cr(value: Any) -> str:
    """Human-readable credits: 16,212,810 -> '16.2M', 49,185 -> '49k'."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    v = int(round(value))
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.0f}k"
    return str(v)


def _fmt(value: Any, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    return f"{int(round(value)):,}{suffix}"


def _ly(value: Any) -> dict[str, Any]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {"type": "text", "value": ""}
    return {"type": "text", "value": str(int(round(value)))}


def _hops(result: Any) -> list[dict[str, Any]]:
    return result.get("hops", []) if isinstance(result, dict) else (result or [])


def total_profit(result: Any) -> int:
    return sum(int(hop.get("total_profit") or 0) for hop in _hops(result))


def total_distance(result: Any) -> int:
    return int(round(sum(float(hop.get("distance") or 0) for hop in _hops(result))))


def hop_tons(hop: dict[str, Any]) -> int:
    return sum(int(c.get("amount") or 0) for c in (hop.get("commodities") or []))


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def squash(value: Any) -> str:
    """Collapse a commodity name to letters+digits only, so Spansh's display names
    ("Liquid oxygen", "Reactive Armour") match the journal's internal Type
    ("liquidoxygen", "reactivearmour") and its localised name alike."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _commodity_label(hop: dict[str, Any]) -> str:
    parts = [f"{c.get('name')} x{c.get('amount')}" for c in (hop.get("commodities") or []) if c.get("name")]
    return " + ".join(parts) or "cargo"


def build_stops(result: Any) -> list[dict[str, Any]]:
    """Station-by-station checklist model: one entry per station you dock at, each with
    what to SELL (cargo carried in), what to BUY (cargo out), and where to FLY next."""
    hops = _hops(result)
    stops: list[dict[str, Any]] = []
    n = len(hops)
    for k in range(n):
        hop = hops[k]
        src = hop.get("source") or {}
        dst = hop.get("destination") or {}
        prev = hops[k - 1] if k > 0 else None
        stops.append({
            "system": src.get("system"), "station": src.get("station"), "hop": k,
            "sell": {squash(c.get("name")) for c in (prev.get("commodities") or [])} if prev else set(),
            "sell_label": _commodity_label(prev) if prev else "",
            "buy": {squash(c.get("name")) for c in (hop.get("commodities") or [])},
            "buy_label": _commodity_label(hop),
            "fly_system": dst.get("system"), "fly_station": dst.get("station"),
        })
    if hops:
        last = hops[-1]
        ldst = last.get("destination") or {}
        stops.append({
            "system": ldst.get("system"), "station": ldst.get("station"), "hop": n - 1,
            "sell": {squash(c.get("name")) for c in (last.get("commodities") or [])},
            "sell_label": _commodity_label(last),
            "buy": set(), "buy_label": "",
            "fly_system": None, "fly_station": None,
        })
    return stops


def build_steps(result: Any) -> list[dict[str, Any]]:
    """A flat Buy -> Fly -> Sell action list for the run checklist. Each step carries
    the hop index, the commodity name-set (normalised) and the system/station it
    happens at, so live journal events (MarketBuy / Docked / MarketSell) can tick it."""
    hops = _hops(result)
    approach = result.get("approach") if isinstance(result, dict) else None
    from_system = result.get("from_system") if isinstance(result, dict) else None
    steps: list[dict[str, Any]] = []
    if approach and approach.get("system") and approach.get("system") != from_system:
        steps.append({
            "kind": "fly", "hop": -1, "commodities": set(),
            "system": approach.get("system"), "station": approach.get("station"),
            "text": f"Travel to {approach.get('system')} / {approach.get('station')} to start",
        })
    for i, hop in enumerate(hops):
        src = hop.get("source") or {}
        dst = hop.get("destination") or {}
        names = {_norm(c.get("name")) for c in (hop.get("commodities") or []) if c.get("name")}
        label = _commodity_label(hop)
        steps.append({
            "kind": "buy", "hop": i, "commodities": names,
            "system": src.get("system"), "station": src.get("station"),
            "text": f"Buy {label}  @ {src.get('system')} / {src.get('station')}",
        })
        steps.append({
            "kind": "fly", "hop": i, "commodities": set(),
            "system": dst.get("system"), "station": dst.get("station"),
            "text": f"Fly to {dst.get('system')} - dock at {dst.get('station')}",
        })
        steps.append({
            "kind": "sell", "hop": i, "commodities": names,
            "system": dst.get("system"), "station": dst.get("station"),
            "text": f"Sell {label}  @ {dst.get('station')}",
        })
    return steps


def build_cargo_grid(
    result: Any, current_hop: int | None = None, skipped: set[int] | None = None
) -> tuple[list[dict[str, Any]], list[int | None]]:
    """Grid rows for the cargo route, ONE ROW PER COMMODITY (hops can carry several
    commodities to fill the hold). Returns (rows, row_hops) where row_hops[i] is the
    hop index for grid row i (None for the leading 'travel to start' row)."""
    hops = _hops(result)
    skipped = skipped or set()
    approach = result.get("approach") if isinstance(result, dict) else None
    from_system = result.get("from_system") if isinstance(result, dict) else None
    rows: list[dict[str, Any]] = []
    row_hops: list[int | None] = []

    if approach and approach.get("system") and approach.get("system") != from_system:
        to_text = f"{approach.get('system')} / {approach.get('station')}"
        tip = (
            "Travel to your first buy station:\r\n"
            f"{to_text}  [{_fmt(approach.get('distance_ly'), ' ly')}, {_fmt(approach.get('distance_ls'), ' ls')}]"
        )
        rows.append({"row": -2, "cells": [
            {"type": "text", "value": ">"},
            {"type": "text", "value": "(travel to start)", "tooltip": tip},
            {"type": "text", "value": str(from_system or "you"), "tooltip": tip},
            {"type": "text", "value": to_text, "tooltip": tip},
            _ly(approach.get("distance_ly")),
            {"type": "text", "value": ""}, {"type": "text", "value": ""},
        ]})
        row_hops.append(None)

    cumulative = 0
    for hi, hop in enumerate(hops):
        commodities = hop.get("commodities") or [{}]
        source = hop.get("source") or {}
        destination = hop.get("destination") or {}
        from_text = f"{source.get('system')} / {source.get('station')}"
        to_text = f"{destination.get('system')} / {destination.get('station')}"
        is_skipped = hi in skipped
        tons = hop_tons(hop)
        for ci, com in enumerate(commodities):
            first = ci == 0
            name = com.get("name", "?")
            amount = com.get("amount")
            buy = (com.get("source_commodity") or {}).get("buy_price")
            sell = (com.get("destination_commodity") or {}).get("sell_price")
            profit = int(com.get("total_profit") or 0)
            if not is_skipped:
                cumulative += profit
            per_ton = (profit / amount) if amount else None
            if first:
                hop_label = f"~{hi + 1}" if is_skipped else (f"> {hi + 1}" if current_hop == hi else str(hi + 1))
            else:
                hop_label = ""
            com_text = name + (f"  x{amount}" if amount else "")
            com_text = (("SKIP " if is_skipped else "") + com_text) if first else ("  + " + com_text)
            tip = "\r\n".join([
                f"Hop {hi + 1}: {name}" + (f" x{amount}" if amount else "") + (f"   (hold {tons}t)" if first else ""),
                f"Buy  {from_text}  ({_fmt(source.get('distance_to_arrival'), ' ls')})  @ {_fmt(buy, ' cr')}",
                f"Sell {to_text}  ({_fmt(destination.get('distance_to_arrival'), ' ls')})  @ {_fmt(sell, ' cr')}",
                f"Jump {_fmt(hop.get('distance'), ' ly')}  |  profit {_fmt(profit, ' cr')}"
                + (f"  ({_fmt(per_ton, ' cr/t')})" if per_ton else ""),
            ])
            rows.append({"row": -2, "cells": [
                {"type": "text", "value": hop_label},
                {"type": "text", "value": com_text, "tooltip": tip},
                {"type": "text", "value": (from_text if first else ""), "tooltip": tip},
                {"type": "text", "value": (to_text if first else ""), "tooltip": tip},
                _ly(hop.get("distance") if first else None),
                {"type": "text", "value": _cr(profit), "tooltip": tip},
                {"type": "text", "value": ("" if is_skipped else _cr(cumulative)), "tooltip": tip},
            ]})
            row_hops.append(hi)
    return rows, row_hops


def route_to_rows(result: Any, current_hop: int | None = None) -> list[dict[str, Any]]:
    hops = _hops(result)
    approach = result.get("approach") if isinstance(result, dict) else None
    from_system = result.get("from_system") if isinstance(result, dict) else None
    rows: list[dict[str, Any]] = []

    # Prepend a "travel to your first buy station" row when the start market is in a
    # different system than where you are.
    if approach and approach.get("system") and approach.get("system") != from_system:
        to_text = f"{approach.get('system')} / {approach.get('station')}"
        tip = (
            "Travel to your first buy station:\r\n"
            f"{to_text}  [{_fmt(approach.get('distance_ly'), ' ly')}, "
            f"{_fmt(approach.get('distance_ls'), ' ls')}]"
        )
        rows.append({
            "row": -2,
            "cells": [
                {"type": "text", "value": ">"},
                {"type": "text", "value": "(travel to start)", "tooltip": tip},
                {"type": "text", "value": str(from_system or "you"), "tooltip": tip},
                {"type": "text", "value": to_text, "tooltip": tip},
                _ly(approach.get("distance_ly")),
                {"type": "text", "value": ""},
                {"type": "text", "value": ""},
            ],
        })

    cumulative = 0
    for index, hop in enumerate(hops, start=1):
        commodities = hop.get("commodities") or []
        first = commodities[0] if commodities else {}
        name = first.get("name", "?")
        amount = first.get("amount")
        source = hop.get("source") or {}
        destination = hop.get("destination") or {}
        buy = (first.get("source_commodity") or {}).get("buy_price")
        sell = (first.get("destination_commodity") or {}).get("sell_price")
        profit = int(hop.get("total_profit") or 0)
        cumulative += profit
        commodity_text = name + (f"  x{amount}" if amount else "")
        from_text = f"{source.get('system')} / {source.get('station')}"
        to_text = f"{destination.get('system')} / {destination.get('station')}"
        per_ton = (profit / amount) if amount else None
        extra = f"\r\n(+{len(commodities) - 1} more commodity on this hop)" if len(commodities) > 1 else ""
        tip = "\r\n".join([
            f"Hop {index}: {commodity_text}",
            f"Buy  {from_text}  ({_fmt(source.get('distance_to_arrival'), ' ls')})  @ {_fmt(buy, ' cr')}",
            f"Sell {to_text}  ({_fmt(destination.get('distance_to_arrival'), ' ls')})  @ {_fmt(sell, ' cr')}",
            f"Jump {_fmt(hop.get('distance'), ' ly')}  |  profit {_fmt(profit, ' cr')}"
            + (f"  ({_fmt(per_ton, ' cr/t')})" if per_ton else ""),
        ]) + extra
        hop_label = f"> {index}" if current_hop == index - 1 else str(index)
        rows.append({
            "row": -2,
            "cells": [
                {"type": "text", "value": hop_label},
                {"type": "text", "value": commodity_text, "tooltip": tip},
                {"type": "text", "value": from_text, "tooltip": tip},
                {"type": "text", "value": to_text, "tooltip": tip},
                _ly(hop.get("distance")),
                {"type": "text", "value": _cr(profit), "tooltip": tip},
                {"type": "text", "value": _cr(cumulative), "tooltip": tip},
            ],
        })
    return rows
