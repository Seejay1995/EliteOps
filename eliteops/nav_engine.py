"""Navigation Assistant engine for EliteOps — ported from the C# NavAssistant scorers.

Turns the live journal into ONE clear recommendation at a time (Complete FSS / Map
body / Cash in / Skip system / Continue), plus the trip's unsold value-at-risk.
Reuses the shared firsts.Radar for the current system's bodies; the value-at-risk
pool is the one piece of session state the radar doesn't keep, so it's tracked here
and persists across jumps until you sell at a station.

Faithful port of:
  FssCompletionScorer, MapHighValueBodyScorer, CashInValueAtRiskScorer,
  SkipSystemScorer, ContinueScorer, ScoringHeuristics.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
import firsts  # noqa: E402

DEFAULT_CASH_IN_THRESHOLD = 20_000_000

_ELW = "Earthlike body"
_WW = "Water world"
_AW = "Ammonia world"


# --- shared heuristics (ScoringHeuristics.cs) -------------------------------
def _has_kind(b) -> bool:
    """A body we have real scan data for (bio-signal stubs have no class)."""
    return bool(b.planet_class or b.star_type)


def _scanned_count(radar) -> int:
    return sum(1 for b in radar.bodies.values() if _has_kind(b))


def _fss_complete(radar) -> bool:
    return radar.body_count is not None and _scanned_count(radar) >= radar.body_count


def _is_worth_mapping(b) -> bool:
    if b.mapped_by_me or not b.is_planet:
        return False
    return (b.terraformable
            or b.planet_class in (_ELW, _WW, _AW)
            or (b.bio_signals or 0) > 0)


def _kind_priority(b) -> int:
    return {_ELW: 4, _WW: 3, _AW: 2}.get(b.planet_class, 0)


def _body_scan_value(e: dict[str, Any]) -> int:
    """Estimated sale value of a single Scan event, reusing the firsts formulas."""
    first = not e.get("WasDiscovered", True)
    if e.get("StarType"):
        mass = e.get("StellarMass")
        return firsts.star_scan_value(e["StarType"], mass, first) if mass is not None else 0
    if e.get("PlanetClass"):
        mass = e.get("MassEM")
        if mass is None:
            return 0
        terra = str(e.get("TerraformState") or "") in ("Terraformable", "Terraforming")
        first_map = not e.get("WasMapped", True)
        return firsts.planet_claim_value(e["PlanetClass"], terra, mass, first, first_map)
    return 0


class NavEngine:
    def __init__(self, state, firsts_engine, cash_in_threshold: int = DEFAULT_CASH_IN_THRESHOLD) -> None:
        self.firsts = firsts_engine          # reuse its radar for current-system bodies
        self.cash_in_threshold = cash_in_threshold
        self._lock = threading.Lock()
        self._unsold_value = 0
        self._valued: dict[str, int] = {}    # body key -> value already counted this trip
        self._charted: dict[str, Any] = {}   # system -> EDSM bodies result / pending marker
        state.add_journal_listener(self.on_journal)

    # --- "is this system already charted?" (EDSM, cached per system) --------
    def _ensure_charted(self, system: str | None) -> Any:
        if not system:
            return None
        cached = self._charted.get(system)
        if cached is not None:
            return None if cached == "pending" else cached
        self._charted[system] = "pending"
        threading.Thread(target=self._fetch_charted, args=(system,),
                         name="eliteops-edsm", daemon=True).start()
        return None

    def _fetch_charted(self, system: str) -> None:
        from . import edsm_client
        result = None
        try:
            result = edsm_client.system_bodies(system)
        except Exception:  # noqa: BLE001
            result = None
        with self._lock:
            self._charted[system] = result or {"charted": False, "known": 0, "unreachable": True}

    # --- value-at-risk pool (SessionEngine's trip pool) --------------------
    def on_journal(self, e: dict[str, Any]) -> None:
        ev = e.get("event") or e.get("EventTypeID")
        with self._lock:
            if ev == "Scan":
                key = f"{e.get('SystemAddress')}:{e.get('BodyID')}"
                if key in self._valued:
                    return
                val = _body_scan_value(e)
                if val > 0:
                    self._valued[key] = val
                    self._unsold_value += val
            elif ev in ("SellExplorationData", "MultiSellExplorationData"):
                # cashed in at a station — the trip pool is banked
                self._unsold_value = 0
                self._valued.clear()

    def set_threshold(self, credits: int) -> None:
        with self._lock:
            self.cash_in_threshold = max(0, int(credits))

    def persist(self) -> dict:
        with self._lock:
            return {"threshold": self.cash_in_threshold}

    def restore(self, data: dict | None) -> None:
        if data and data.get("threshold") is not None:
            self.set_threshold(int(data["threshold"]))

    # --- scorers (one method each, faithful to the C#) --------------------
    def _score_fss(self, radar, charted) -> dict | None:
        if not radar.system:
            return None
        scanned = _scanned_count(radar)
        total = radar.body_count
        if total is not None and scanned >= total:
            return None
        # Don't burn 10 minutes FSSing a system the galaxy already has fully charted —
        # its bodies are known (and none are first-discoveries anyway).
        if charted and charted.get("charted"):
            return None
        if total is None:
            frac, detail = 1.0, "Run the discovery scanner to reveal this system."
        else:
            remaining = total - scanned
            frac = remaining / max(1, total)
            detail = f"{remaining} of {total} bodies still need scanning."
        return _rec("CompleteFss", "Complete the FSS", detail,
                    0.55 + 0.20 * frac, "Confirmed")

    def _score_charted(self, radar, charted) -> dict | None:
        """When EDSM already knows the whole system, skip the FSS: point straight at
        what's worth mapping, or advise moving on."""
        if not radar.system or not charted or not charted.get("charted"):
            return None
        # if the player has already locally scanned it, the normal scorers cover it
        if _fss_complete(radar):
            return None
        worth = charted.get("worth_mapping") or []
        unmapped = [w for w in worth if not w.get("isMapped")]
        if unmapped:
            names = ", ".join(w["name"] for w in unmapped[:3])
            detail = (f"Already charted in EDSM — {len(unmapped)} worth-mapping "
                      f"({names}). Map those directly; no need to FSS.")
            return _rec("MapBody", "Map the charted highlights", detail, 0.62, "Confirmed")
        detail = (f"This system is fully charted in EDSM ({charted.get('known')} bodies) "
                  "and nothing is high-value. Skip it — don't bother FSSing.")
        return _rec("SkipSystem", "Skip — already charted", detail, 0.45, "Confirmed")

    def _score_map(self, radar) -> dict | None:
        candidates = [b for b in radar.bodies.values() if _is_worth_mapping(b)]
        if not candidates:
            return None
        target = sorted(candidates, key=lambda b: (
            -(b.bio_signals or 0),
            -_kind_priority(b),
            -(b.value() or 0),
            b.distance_ls if b.distance_ls is not None else 1e18,
        ))[0]
        score = 0.60
        if target.planet_class == _ELW:
            score += 0.08
        elif target.planet_class in (_WW, _AW):
            score += 0.04
        if (target.bio_signals or 0) > 0:
            score += 0.05
        why = _describe_map(target)
        return _rec("MapBody", f"Map {target.name}", why, score, "Confirmed",
                    body=target.name)

    def _score_cash_in(self) -> dict | None:
        if self.cash_in_threshold <= 0 or self._unsold_value < self.cash_in_threshold:
            return None
        over = self._unsold_value / self.cash_in_threshold
        score = min(0.99, 0.85 + 0.03 * (over - 1))
        n = len(self._valued)
        detail = (f"You're carrying {self._unsold_value:,.0f} cr of unsold data across "
                  f"{n} bodies. Bank it before you risk it.")
        return _rec("CashInDiscoveries", "Cash in your discoveries", detail, score, "Confirmed")

    def _score_skip(self, radar) -> dict | None:
        if not radar.system or not _fss_complete(radar):
            return None
        if any(_is_worth_mapping(b) for b in radar.bodies.values()):
            return None
        detail = f"All {radar.body_count} bodies scanned and nothing high-value remains here."
        return _rec("SkipSystem", "Skip this system", detail, 0.40, "Probable")

    @staticmethod
    def _score_continue() -> dict:
        return _rec("Continue", "Continue exploring",
                    "Nothing here needs a decision right now.", 0.01, "Estimated")

    # --- snapshot for the UI ----------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        radar = self.firsts.radar
        with self._lock:
            charted = self._ensure_charted(radar.system)
            recs = [
                self._score_cash_in(),
                self._score_fss(radar, charted),
                self._score_charted(radar, charted),
                self._score_map(radar),
                self._score_skip(radar),
                self._score_continue(),
            ]
            recs = [r for r in recs if r]
            recs.sort(key=lambda r: r["score"], reverse=True)
            primary = recs[0] if recs else None
            alternatives = recs[1:]

            scanned = _scanned_count(radar)
            total = radar.body_count
            targets = sorted(
                (b for b in radar.bodies.values() if _is_worth_mapping(b)),
                key=lambda b: (-(b.bio_signals or 0), -_kind_priority(b), -(b.value() or 0)),
            )
            target_rows = [{
                "name": b.name, "kind": b.kind,
                "terraformable": b.terraformable,
                "bio": b.bio_signals or 0,
                "value": b.value(),
                "distance_ls": b.distance_ls,
                "mapped_by_me": b.mapped_by_me,
            } for b in targets]

            charted_out = None
            if isinstance(charted, dict):
                charted_out = {"charted": bool(charted.get("charted")),
                               "known": charted.get("known"), "bodyCount": charted.get("bodyCount"),
                               "worth_mapping": charted.get("worth_mapping") or []}
            return {
                "system": radar.system or None,
                "fss": {"scanned": scanned, "total": total, "complete": _fss_complete(radar)},
                "charted": charted_out,
                "value_at_risk": {
                    "unsold_value": self._unsold_value,
                    "unsold_bodies": len(self._valued),
                    "threshold": self.cash_in_threshold,
                },
                "primary": primary,
                "alternatives": alternatives,
                "targets": target_rows,
            }


def _rec(kind: str, title: str, detail: str, score: float, confidence: str,
         body: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "title": title, "detail": detail,
            "score": round(score, 3), "confidence": confidence, "body": body}


def _describe_map(b) -> str:
    kind = {
        _ELW: "an Earth-like world", _WW: "a water world", _AW: "an ammonia world",
    }.get(b.planet_class, "a high-value body")
    terra = " terraformable" if b.terraformable else ""
    bio = f" with {b.bio_signals} biological signal(s)" if (b.bio_signals or 0) > 0 else ""
    return f"{b.name} is{terra} {kind}{bio} — worth a surface map."
