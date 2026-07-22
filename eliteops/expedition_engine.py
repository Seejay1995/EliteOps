"""Expeditions for EliteOps — mission-style multi-waypoint journeys.

An expedition is an ordered list of WAYPOINTS (systems), each carrying OBJECTIVES
(sample this species, scan this body, just arrive). It ticks itself off as you fly:
jumping into a waypoint marks it arrived and copies the next system to your clipboard;
analysing a species or mapping a body ticks that objective.

Built from any route source — the Exo tab's route, Road to Riches, a neutron plot, a
tourist multi-stop plot, or a pasted list of systems. Generalises the cargo checklist
pattern in cargo_engine.py.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

sys_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
import sys  # noqa: E402

sys.path.insert(0, sys_path)
import spansh_client  # noqa: E402

try:
    import clipboard_service  # noqa: E402
except Exception:  # noqa: BLE001 - clipboard is best-effort
    clipboard_service = None  # type: ignore

_EXPEDITIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "expeditions")


def _norm(v: Any) -> str:
    return "".join(c for c in str(v or "").lower() if c.isalnum())


class ExpeditionEngine:
    def __init__(self, state, exo_engine=None) -> None:
        self.state = state
        self.exo = exo_engine
        self._lock = threading.Lock()
        self._name = ""
        self._source = ""
        self._waypoints: list[dict] = []
        self._current = 0
        self._status = "idle"          # idle | plotting | ready | error
        self._error = ""
        self._note = ""
        state.add_journal_listener(self.on_journal)

    # --- waypoint construction ---------------------------------------------
    @staticmethod
    def _wp(system: str, objectives: list[dict], meta: dict | None = None) -> dict:
        return {"system": system, "objectives": objectives, "meta": meta or {},
                "arrived": False, "skipped": False}

    @staticmethod
    def _obj(kind: str, text: str, target: str = "", value: Any = None) -> dict:
        return {"kind": kind, "text": text, "target": target, "value": value, "done": False}

    def _install(self, name: str, source: str, waypoints: list[dict], note: str = "") -> None:
        with self._lock:
            self._name = name
            self._source = source
            self._waypoints = waypoints
            self._current = 0
            self._status = "ready" if waypoints else "idle"
            self._error = ""
            self._note = note

    # --- builders (one per route source) -----------------------------------
    def from_exo(self, route: dict, name: str = "") -> int:
        """Exo route -> a waypoint per system, an objective per species."""
        wps = []
        for s in (route or {}).get("systems") or []:
            objs = []
            for b in s.get("bodies") or []:
                for sp in b.get("species") or []:
                    genus, species = (sp.get("genus") or ""), (sp.get("species") or "")
                    # Spansh's species usually already carries the genus ("Stratum Tectonicas")
                    if species and genus and _norm(species).startswith(_norm(genus)):
                        label = species
                    else:
                        label = " ".join(x for x in (genus, species) if x) or "Unknown species"
                    objs.append(self._obj("species", f"Sample {label} — {b.get('name')}",
                                          target=_norm(sp.get("species") or sp.get("genus")),
                                          value=sp.get("value")))
            if objs:
                wps.append(self._wp(s.get("name"), objs,
                                    {"value": s.get("total_value"), "jumps": s.get("jumps")}))
        self._install(name or "Exobiology expedition", "exo", wps,
                      f"{(route or {}).get('total_value') or 0:,} cr of biology")
        return len(wps)

    def from_riches(self, route: dict, name: str = "") -> int:
        """Road to Riches -> a waypoint per system, an objective per body to scan/map."""
        wps = []
        for s in (route or {}).get("systems") or []:
            objs = [self._obj("body", f"Scan / map {b.get('name')} ({b.get('subtype')})",
                              target=_norm(b.get("name")), value=b.get("value"))
                    for b in s.get("bodies") or []]
            if objs:
                wps.append(self._wp(s.get("name"), objs,
                                    {"value": s.get("total_value"), "jumps": s.get("jumps")}))
        self._install(name or "Road to Riches", "riches", wps,
                      f"{(route or {}).get('total_value') or 0:,} cr of bodies")
        return len(wps)

    def from_jumps(self, route: dict, name: str = "") -> int:
        """Neutron / tourist plot -> a waypoint per hop, objective = arrive."""
        kind = (route or {}).get("kind") or "plot"
        wps = []
        for w in (route or {}).get("waypoints") or []:
            label = "Arrive at " + str(w.get("system"))
            if w.get("neutron"):
                label += "  ★ neutron — boost here"
            wps.append(self._wp(w.get("system"), [self._obj("arrive", label)],
                                {"jumps": w.get("jumps"), "neutron": w.get("neutron"),
                                 "distance_left": w.get("distance_left")}))
        if kind == "neutron":
            note = (f"{route.get('total_jumps') or '?'} jumps · "
                    f"{round(route.get('distance') or 0):,} ly to {route.get('to_system')}")
            default = f"Neutron run to {route.get('to_system')}"
        else:
            note = f"{len(wps)} stops"
            default = "Tourist run"
        self._install(name or default, kind, wps, note)
        return len(wps)

    def from_systems(self, text: str, name: str = "", objective: str = "") -> int:
        """A pasted list of systems -> one waypoint each with a free-text objective."""
        wps = []
        for line in str(text or "").splitlines():
            line = line.strip().lstrip("-•*").strip()
            if not line:
                continue
            # tolerate "System — do a thing" / "System | note"
            parts = re.split(r"\s+[—|]\s+", line, maxsplit=1)
            system = parts[0].strip()
            task = parts[1].strip() if len(parts) > 1 else (objective or "Visit")
            wps.append(self._wp(system, [self._obj("custom", task)]))
        self._install(name or "Expedition", "manual", wps, f"{len(wps)} systems")
        return len(wps)

    # --- plotting (threaded Spansh calls) ----------------------------------
    def plot(self, kind: str, params: dict) -> None:
        with self._lock:
            self._status = "plotting"
            self._error = ""
        threading.Thread(target=self._run_plot, args=(kind, params or {}),
                         name="eliteops-expedition-plot", daemon=True).start()

    def _run_plot(self, kind: str, p: dict) -> None:
        try:
            jr = float(p.get("jump_range") or 50)
            if kind == "neutron":
                route = spansh_client.neutron_route(
                    from_system=p.get("from_system") or self._here(), to_system=p.get("to_system"),
                    jump_range=jr, efficiency=int(p.get("efficiency") or 60))
                self.from_jumps(route, p.get("name") or "")
            elif kind == "tourist":
                dests = p.get("destinations")
                if isinstance(dests, str):
                    dests = [d.strip() for d in dests.splitlines() if d.strip()]
                route = spansh_client.tourist_route(
                    source=p.get("from_system") or self._here(), destinations=dests or [], jump_range=jr)
                self.from_jumps(route, p.get("name") or "")
            elif kind == "riches":
                route = spansh_client.riches_route(
                    from_system=p.get("from_system") or self._here(), jump_range=jr,
                    radius=float(p.get("radius") or 100), max_results=int(p.get("max_results") or 20),
                    min_value=int(p.get("min_value") or 300_000), loop=bool(p.get("loop", False)))
                self.from_riches(route, p.get("name") or "")
            else:
                raise ValueError(f"Unknown plot kind '{kind}'.")
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._status = "error"
                self._error = str(exc)

    def _here(self) -> str:
        return self.state.snapshot().get("system") or ""

    def use_exo_route(self, name: str = "") -> int:
        """Ingest the Exo tab's currently generated route."""
        route = (self.exo.snapshot().get("route") if self.exo else None)
        if not route or not route.get("systems"):
            raise ValueError("No exobiology route yet — generate one on the Exo tab first.")
        return self.from_exo(route, name)

    # --- live auto-tick -----------------------------------------------------
    def on_journal(self, e: dict) -> None:
        ev = e.get("event")
        with self._lock:
            if not self._waypoints:
                return
            if ev in ("FSDJump", "CarrierJump", "Location"):
                self._arrive(e.get("StarSystem"))
            elif ev == "ScanOrganic" and str(e.get("ScanType")) == "Analyse":
                self._tick_target(("species",),
                                  _norm(e.get("Species_Localised") or e.get("Species")))
            elif ev in ("SAAScanComplete", "Scan"):
                self._tick_target(("body",), _norm(e.get("BodyName")))

    def _arrive(self, system: Any) -> None:
        target = _norm(system)
        if not target:
            return
        for i, w in enumerate(self._waypoints):
            if _norm(w["system"]) == target:
                w["arrived"] = True
                self._current = i
                nxt = self._next_system(i)
                if nxt:
                    self._copy(nxt)
                return

    def _tick_target(self, kinds: tuple, target: str) -> None:
        """Tick a matching objective — prefer the current waypoint, else search all."""
        if not target:
            return
        order = [self._current] + [i for i in range(len(self._waypoints)) if i != self._current]
        for i in order:
            for o in self._waypoints[i]["objectives"]:
                if o["done"] or o["kind"] not in kinds or not o["target"]:
                    continue
                if o["target"] == target or o["target"] in target or target in o["target"]:
                    o["done"] = True
                    return

    def _next_system(self, index: int) -> str:
        for w in self._waypoints[index + 1:]:
            if not w["skipped"]:
                return w["system"]
        return ""

    def _copy(self, text: str) -> bool:
        if not text or clipboard_service is None:
            return False
        try:
            return bool(getattr(clipboard_service.copy_text(str(text)), "success", False))
        except Exception:  # noqa: BLE001
            return False

    # --- user actions -------------------------------------------------------
    def goto(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._waypoints):
                self._current = index

    def skip(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._waypoints):
                w = self._waypoints[index]
                w["skipped"] = not w["skipped"]

    def toggle_objective(self, wp: int, obj: int) -> None:
        with self._lock:
            if 0 <= wp < len(self._waypoints):
                objs = self._waypoints[wp]["objectives"]
                if 0 <= obj < len(objs):
                    objs[obj]["done"] = not objs[obj]["done"]

    def copy_system(self, system: str) -> bool:
        with self._lock:
            return self._copy(system)

    def rename(self, name: str) -> None:
        with self._lock:
            self._name = str(name or "").strip() or self._name

    def reset(self) -> None:
        with self._lock:
            self._name = self._source = self._error = self._note = ""
            self._waypoints = []
            self._current = 0
            self._status = "idle"

    # --- named save / load --------------------------------------------------
    @staticmethod
    def _slug(name: str) -> str:
        s = "".join(c if c.isalnum() else "-" for c in str(name or "").lower()).strip("-")
        return re.sub(r"-+", "-", s) or "expedition"

    def list_saved(self) -> list[dict]:
        out = []
        try:
            for fn in os.listdir(_EXPEDITIONS_DIR):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(_EXPEDITIONS_DIR, fn), encoding="utf-8") as fh:
                        d = json.load(fh)
                    out.append({"slug": fn[:-5], "name": d.get("name") or fn[:-5],
                                "source": d.get("source"),
                                "waypoints": len(d.get("waypoints") or [])})
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        out.sort(key=lambda x: x["name"].lower())
        return out

    def save(self, name: str | None = None) -> dict:
        with self._lock:
            if not self._waypoints:
                raise ValueError("Nothing to save — make an expedition first.")
            if name:
                self._name = name
            data = self._persist_data()
            slug = self._slug(self._name)
        os.makedirs(_EXPEDITIONS_DIR, exist_ok=True)
        with open(os.path.join(_EXPEDITIONS_DIR, slug + ".json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return {"slug": slug, "name": data["name"]}

    def load(self, slug: str) -> bool:
        path = os.path.join(_EXPEDITIONS_DIR, self._slug(slug) + ".json")
        if not os.path.isfile(path):
            return False
        with open(path, encoding="utf-8") as fh:
            self.restore(json.load(fh))
        return True

    def delete(self, slug: str) -> None:
        try:
            os.remove(os.path.join(_EXPEDITIONS_DIR, self._slug(slug) + ".json"))
        except OSError:
            pass

    # --- persistence --------------------------------------------------------
    def _persist_data(self) -> dict:
        return {"name": self._name, "source": self._source, "note": self._note,
                "current": self._current, "status": self._status,
                "waypoints": self._waypoints}

    def persist(self) -> dict:
        with self._lock:
            return self._persist_data()

    def restore(self, data: dict | None) -> None:
        if not data or not data.get("waypoints"):
            return
        with self._lock:
            self._name = data.get("name") or "Expedition"
            self._source = data.get("source") or ""
            self._note = data.get("note") or ""
            self._waypoints = data.get("waypoints") or []
            self._current = int(data.get("current") or 0)
            self._status = data.get("status") or "ready"
            self._error = ""

    # --- snapshot -----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            wps, done_wp, obj_done, obj_total, value = [], 0, 0, 0, 0
            cur = min(self._current, len(self._waypoints) - 1) if self._waypoints else 0
            for i, w in enumerate(self._waypoints):
                objs = w["objectives"]
                d = sum(1 for o in objs if o["done"])
                complete = bool(objs) and d == len(objs)
                if complete or w["skipped"]:
                    done_wp += 1
                obj_done += d
                obj_total += len(objs)
                value += int(w["meta"].get("value") or 0)
                wps.append({
                    "n": i + 1, "system": w["system"], "objectives": objs,
                    "meta": w["meta"], "arrived": w["arrived"], "skipped": w["skipped"],
                    "complete": complete, "current": i == cur,
                    "objectives_done": d, "objectives_total": len(objs),
                })
            total = len(self._waypoints)
            nxt = self._next_system(cur) if self._waypoints else ""
            return {
                "status": self._status, "error": self._error,
                "name": self._name, "source": self._source, "note": self._note,
                "current": cur, "next_system": nxt,
                "progress": {
                    "waypoints_done": done_wp, "waypoints_total": total,
                    "pct": round(100 * done_wp / total) if total else 0,
                    "objectives_done": obj_done, "objectives_total": obj_total,
                    "value": value,
                    "distance_left": (self._waypoints[cur]["meta"].get("distance_left")
                                      if self._waypoints else None),
                },
                "waypoints": wps,
                "saved": self.list_saved(),
            }
