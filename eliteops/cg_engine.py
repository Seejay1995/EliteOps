"""Community Goals for EliteOps.

Two sources, merged:
  * Journal `CommunityGoal` event (zero-config, has YOUR contribution/rank/percentile).
  * INARA `getCommunityGoalsRecent` (optional, needs a free API key) — always-live full
    list even if you haven't checked a board, plus objective + reward text.

When an INARA key is set we use its live list as the base and overlay your personal
standing from the journal (matched by CG id). The key is stored locally only and is
never returned by the API.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .state import DEFAULT_JOURNAL_DIR

_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "session", "config.json")
_INARA_URL = "https://inara.cz/inapi/v1/"
_INARA_TTL = 900  # re-fetch INARA at most every 15 min (CGs move slowly)


def _tier(n):
    if isinstance(n, str):
        return n
    return f"Tier {n}" if n is not None else None


class CGEngine:
    def __init__(self, state) -> None:
        self._lock = threading.Lock()
        self._goals: list[dict] = []          # journal CurrentGoals
        self._updated: str | None = None
        self._inara_key = self._load_key()
        self._inara_goals: list[dict] = []
        self._inara_updated: float | None = None
        self._inara_status = "idle"
        self._inara_error = ""
        self._fetching = False
        self._seed_from_history(getattr(state, "dir", DEFAULT_JOURNAL_DIR))
        state.add_journal_listener(self.on_journal)
        if self._inara_key:
            self._kick_inara()

    # --- config (API key stored locally, never exposed) --------------------
    def _load_key(self) -> str:
        try:
            with open(_CONFIG, encoding="utf-8") as fh:
                return str(json.load(fh).get("inara_api_key") or "")
        except (OSError, ValueError):
            return ""

    def _save_key(self, key: str) -> None:
        data = {}
        try:
            with open(_CONFIG, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            pass
        data["inara_api_key"] = key
        try:
            os.makedirs(os.path.dirname(_CONFIG), exist_ok=True)
            with open(_CONFIG, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass

    def set_inara_key(self, key: str) -> None:
        key = str(key or "").strip()
        with self._lock:
            self._inara_key = key
            self._save_key(key)
            self._inara_goals = []
            self._inara_updated = None
            self._inara_status = "idle" if key else "off"
        if key:
            self._kick_inara(force=True)

    # --- journal source ----------------------------------------------------
    def _seed_from_history(self, journal_dir: str) -> None:
        try:
            files = sorted(glob.glob(os.path.join(journal_dir, "Journal.*.log")),
                           key=os.path.getmtime, reverse=True)[:6]
        except OSError:
            return
        for path in files:
            latest = None
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"CommunityGoal"' not in line:
                            continue
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if e.get("event") == "CommunityGoal":
                            latest = e
            except OSError:
                continue
            if latest:
                self._apply(latest)
                return

    def on_journal(self, e: dict) -> None:
        if e.get("event") == "CommunityGoal":
            self._apply(e)

    def _apply(self, e: dict) -> None:
        with self._lock:
            self._goals = e.get("CurrentGoals") or []
            self._updated = e.get("timestamp")

    # --- INARA source ------------------------------------------------------
    def _kick_inara(self, force: bool = False) -> None:
        with self._lock:
            if self._fetching or not self._inara_key:
                return
            fresh = self._inara_updated and (time.time() - self._inara_updated) < _INARA_TTL
            if fresh and not force:
                return
            self._fetching = True
            self._inara_status = "fetching"
        threading.Thread(target=self._fetch_inara, name="eliteops-inara", daemon=True).start()

    def _fetch_inara(self) -> None:
        try:
            body = {
                "header": {"appName": "EliteOps", "appVersion": "1.0",
                           "isBeingDeveloped": True, "APIkey": self._inara_key},
                "events": [{"eventName": "getCommunityGoalsRecent",
                            "eventTimestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "eventData": []}],
            }
            req = urllib.request.Request(
                _INARA_URL, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "EliteOps/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            hstatus = ((data.get("header") or {}).get("eventStatus"))
            if hstatus and int(hstatus) >= 400:
                raise ValueError((data.get("header") or {}).get("eventStatusText") or "INARA rejected the key")
            goals = []
            for ev in data.get("events") or []:
                if ev.get("eventName") != "getCommunityGoalsRecent":
                    continue
                if int(ev.get("eventStatus") or 0) >= 400:
                    raise ValueError(ev.get("eventStatusText") or "INARA error")
                for g in ev.get("eventData") or []:
                    goals.append({
                        "id": g.get("communitygoalGameID"),
                        "title": g.get("communitygoalName"),
                        "system": g.get("starsystemName"),
                        "station": g.get("stationName"),
                        "expiry": g.get("goalExpiry"),
                        "complete": bool(g.get("isCompleted")),
                        "tier": _tier(g.get("tierReached")),
                        "top_tier": _tier(g.get("tierMax")),
                        "total": g.get("contributionsTotal"),
                        "contributors": g.get("contributorsNum"),
                        "top_rank_size": g.get("topRankSize"),
                        "objective": g.get("goalObjectiveText"),
                        "reward_text": g.get("goalRewardText"),
                        "war_zone": bool(g.get("isWarZone")),
                    })
            with self._lock:
                self._inara_goals = goals
                self._inara_updated = time.time()
                self._inara_status = "ok"
                self._inara_error = ""
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._inara_status = "error"
                self._inara_error = str(exc)[:160]
        finally:
            with self._lock:
                self._fetching = False

    # --- snapshot ----------------------------------------------------------
    def _journal_goal(self, cgid) -> dict:
        for g in self._goals:
            if g.get("CGID") == cgid:
                return g
        return {}

    def _from_journal(self, g: dict) -> dict:
        top = g.get("TopTier") or {}
        return {
            "id": g.get("CGID"), "title": g.get("Title"), "system": g.get("SystemName"),
            "station": g.get("MarketName"), "expiry": g.get("Expiry"),
            "complete": bool(g.get("IsComplete")), "tier": _tier(g.get("TierReached")),
            "top_tier": top.get("Name"), "total": g.get("CurrentTotal"),
            "contributors": g.get("NumContributors"), "bonus": g.get("Bonus"),
            "top_rank_size": g.get("TopRankSize"),
            "your_contribution": g.get("PlayerContribution"),
            "percentile": g.get("PlayerPercentileBand"),
            "in_top_rank": bool(g.get("PlayerInTopRank")),
        }

    def snapshot(self) -> dict:
        if self._inara_key:
            self._kick_inara()
        with self._lock:
            if self._inara_key and self._inara_goals:
                goals = []
                for g in self._inara_goals:
                    jg = self._journal_goal(g.get("id"))
                    goals.append({**g,
                                  "your_contribution": jg.get("PlayerContribution"),
                                  "percentile": jg.get("PlayerPercentileBand"),
                                  "in_top_rank": bool(jg.get("PlayerInTopRank"))})
                source, updated = "INARA", (datetime.fromtimestamp(self._inara_updated, timezone.utc)
                                            .strftime("%Y-%m-%dT%H:%M:%SZ") if self._inara_updated else None)
            else:
                goals = [self._from_journal(g) for g in self._goals]
                source, updated = "journal", self._updated
            return {
                "goals": goals, "updated": updated, "source": source,
                "inara_configured": bool(self._inara_key),
                "inara_status": self._inara_status, "inara_error": self._inara_error,
            }
