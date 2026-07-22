"""Live Elite Dangerous game state, read straight from local files.

No EDDiscovery required: we tail the newest Journal.*.log for events and read the
game's sidecar JSON files (Status/Cargo/NavRoute/Market) that Frontier writes into
the same folder. This is the always-available Tier-1 data source for the dashboard.
"""

from __future__ import annotations

import glob
import json
import os
import threading
from typing import Any

DEFAULT_JOURNAL_DIR = os.path.join(
    os.path.expanduser("~"), "Saved Games", "Frontier Developments", "Elite Dangerous"
)

# Status.json Flags bits we care about (there are many more).
_FLAG_DOCKED = 1 << 0
_FLAG_LANDED = 1 << 1
_FLAG_SUPERCRUISE = 1 << 4
_FLAG_FSD_JUMP = 1 << 30


def journal_files(journal_dir: str) -> list[str]:
    files = glob.glob(os.path.join(journal_dir, "Journal.*.log"))
    return sorted(files, key=os.path.getmtime)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        return json.loads(data.decode("utf-8", "replace")) if data.strip() else None
    except (OSError, ValueError):
        return None


class EliteState:
    """Rolling game state. Call poll() periodically; read snapshot() for the UI."""

    def __init__(self, journal_dir: str | None = None) -> None:
        self.dir = journal_dir or DEFAULT_JOURNAL_DIR
        self._lock = threading.Lock()
        self._journal_path: str | None = None
        self._offset = 0
        self._status_mtime = 0.0
        self._cargo_mtime = 0.0
        # state
        self.commander: str | None = None
        self.system: str | None = None
        self.system_address: int | None = None
        self.station: str | None = None
        self.docked = False
        self.ship: str | None = None
        self.status: dict[str, Any] = {}
        self.cargo: list[dict[str, Any]] = []
        self.cargo_capacity: int | None = None
        self.jump_range: float | None = None
        # listeners get (event_dict) for each new live journal event
        self._journal_listeners: list = []

    def add_journal_listener(self, callback) -> None:
        self._journal_listeners.append(callback)

    # --- polling ------------------------------------------------------------
    def poll(self) -> bool:
        changed = False
        for event in self._read_new_journal():
            self._apply_event(event)
            for cb in self._journal_listeners:
                try:
                    cb(event)
                except Exception:  # noqa: BLE001 - a listener must not break polling
                    pass
            changed = True
        if self._read_status():
            changed = True
        if self._read_cargo():
            changed = True
        return changed

    def _read_new_journal(self) -> list[dict[str, Any]]:
        latest = None
        files = journal_files(self.dir)
        if files:
            latest = files[-1]
        if latest and latest != self._journal_path:
            self._journal_path = latest
            self._offset = 0
        if not self._journal_path:
            return []
        try:
            with open(self._journal_path, "rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
        except OSError:
            return []
        nl = data.rfind(b"\n")
        if nl == -1:
            return []
        consume = data[: nl + 1]
        self._offset += len(consume)
        events = []
        for raw in consume.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw.decode("utf-8", "replace")))
            except (ValueError, TypeError):
                continue
        return events

    def _apply_event(self, e: dict[str, Any]) -> None:
        ev = e.get("event")
        with self._lock:
            if ev in ("Commander", "LoadGame"):
                self.commander = e.get("Name") or self.commander
                self.ship = e.get("Ship_Localised") or e.get("Ship") or self.ship
            elif ev in ("FSDJump", "CarrierJump", "Location"):
                self.system = e.get("StarSystem") or self.system
                self.system_address = e.get("SystemAddress") or self.system_address
                if ev != "Location":
                    self.station = None
                    self.docked = False
                if e.get("Docked"):
                    self.docked = True
                    self.station = e.get("StationName") or self.station
            elif ev == "Docked":
                self.docked = True
                self.station = e.get("StationName") or self.station
                self.system = e.get("StarSystem") or self.system
            elif ev == "Undocked":
                self.docked = False
                self.station = None
            elif ev == "Loadout":
                self.ship = e.get("Ship_Localised") or e.get("Ship") or self.ship
                cap = e.get("CargoCapacity")
                if isinstance(cap, int):
                    self.cargo_capacity = cap
                jr = e.get("MaxJumpRange")
                if isinstance(jr, (int, float)):
                    self.jump_range = float(jr)

    def _read_status(self) -> bool:
        path = os.path.join(self.dir, "Status.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        if mtime == self._status_mtime:
            return False
        self._status_mtime = mtime
        data = _read_json(path)
        if not isinstance(data, dict):
            return False
        with self._lock:
            self.status = data
            flags = data.get("Flags", 0) or 0
            if isinstance(flags, int):
                self.docked = bool(flags & _FLAG_DOCKED) or self.docked
            if data.get("BodyName"):
                pass  # kept in status
        return True

    def _read_cargo(self) -> bool:
        path = os.path.join(self.dir, "Cargo.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        if mtime == self._cargo_mtime:
            return False
        self._cargo_mtime = mtime
        data = _read_json(path)
        if not isinstance(data, dict):
            return False
        with self._lock:
            self.cargo = data.get("Inventory", []) or []
        return True

    # --- read for the UI ----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            fuel = self.status.get("Fuel") if isinstance(self.status, dict) else None
            fuel_main = fuel.get("FuelMain") if isinstance(fuel, dict) else fuel
            cargo_tons = sum(int(item.get("Count", 0)) for item in self.cargo)
            return {
                "commander": self.commander,
                "system": self.system,
                "station": self.station,
                "docked": self.docked,
                "ship": self.ship,
                "fuel_main": fuel_main,
                "cargo_tons": cargo_tons,
                "cargo_capacity": self.cargo_capacity,
                "jump_range": self.jump_range,
                "cargo": list(self.cargo),
                "body": self.status.get("BodyName") if isinstance(self.status, dict) else None,
            }
