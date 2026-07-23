"""EliteOps dashboard web server (standard library only).

A ThreadingHTTPServer that:
  - serves the responsive SPA from web/
  - exposes /api/state with the live game snapshot
  - runs a background loop polling the journal + sidecar files
Binds 0.0.0.0 so a tablet/phone on the LAN can open it too. No pip dependencies.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from urllib.parse import parse_qs, urlparse

from . import catalog
from .cargo_engine import CargoEngine
from .cg_engine import CGEngine
from .colony_engine import ColonyEngine
from .edd_bridge import EddBridge
from .engmat_engine import EngMatEngine
from .exo_engine import ExoEngine
from .expedition_engine import ExpeditionEngine
from .firsts_engine import FirstsEngine
from .guardian_engine import GuardianEngine
from .mining_engine import MiningEngine
from .nav_engine import NavEngine
from .shipwright_engine import ShipwrightEngine
from .state import EliteState
from .system_engine import SystemEngine

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
SESSION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")
POLL_SECONDS = 1.0
SAVE_EVERY = 8  # poll ticks between session saves


class SessionStore:
    """Persists the non-journal-derived engine state (generated routes, active
    Shipwright build, Guardian picks, cash-in threshold) so a server crash/restart
    doesn't lose your work. Journal-derived state rebuilds from the journal replay."""

    _ENGINES = ("cargo", "exo", "expedition", "shipwright", "guardian", "nav")

    def __init__(self, app: "EliteOps") -> None:
        self.app = app

    def restore(self) -> None:
        try:
            with open(SESSION_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for key in self._ENGINES:
            try:
                getattr(self.app, key).restore(data.get(key))
            except Exception:  # noqa: BLE001 - a bad slice must not break startup
                pass

    def save(self) -> None:
        data = {"version": 1}
        for key in self._ENGINES:
            try:
                data[key] = getattr(self.app, key).persist()
            except Exception:  # noqa: BLE001
                data[key] = None
        try:
            os.makedirs(SESSION_DIR, exist_ok=True)
            tmp = SESSION_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, SESSION_FILE)  # atomic — never leaves a half-written file
        except OSError:
            pass

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
    ".ico": "image/x-icon", ".png": "image/png",
}


class EliteOps:
    def __init__(self, journal_dir: str | None = None) -> None:
        self.state = EliteState(journal_dir)
        self.cargo = CargoEngine(self.state)
        self.firsts = FirstsEngine(self.state)
        self.nav = NavEngine(self.state, self.firsts)
        self.exo = ExoEngine(self.state)
        self.expedition = ExpeditionEngine(self.state, self.exo)
        self.guardian = GuardianEngine(self.state)
        self.engmat = EngMatEngine(self.state)
        self.cg = CGEngine(self.state)
        self.colony = ColonyEngine(self.state)
        self.mining = MiningEngine(self.state)
        self.system = SystemEngine(self.state)
        self.shipwright = ShipwrightEngine(self.state)
        self.edd = EddBridge()
        self.store = SessionStore(self)
        self._stop = threading.Event()

    def _poll_loop(self) -> None:
        ticks = 0
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.state.poll()
            except Exception:  # noqa: BLE001 - a bad file must not kill the loop
                pass
            ticks += 1
            if ticks % SAVE_EVERY == 0:
                self.store.save()

    def serve(self, host: str = "0.0.0.0", port: int = 8384) -> None:
        # prime the journal (rebuilds live state) BEFORE restoring saved routes/build,
        # so a restored cargo checklist isn't clobbered by the replay.
        try:
            self.state.poll()
        except Exception:  # noqa: BLE001
            pass
        self.store.restore()
        threading.Thread(target=self._poll_loop, name="eliteops-poll", daemon=True).start()
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _send(self, code, body, ctype="application/json"):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _read_json_body(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if not length:
                    return {}
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                    return data if isinstance(data, dict) else {}
                except ValueError:
                    return {}

            def do_GET(self):
                parsed = urlparse(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                if path == "/api/state":
                    self._send(200, json.dumps(app.state.snapshot()))
                    return
                if path == "/api/cargo":
                    self._send(200, json.dumps(app.cargo.snapshot()))
                    return
                if path == "/api/cargo/prefill":
                    self._send(200, json.dumps(app.cargo.prefill()))
                    return
                if path == "/api/firsts":
                    self._send(200, json.dumps(app.firsts.snapshot()))
                    return
                if path == "/api/nav":
                    self._send(200, json.dumps(app.nav.snapshot()))
                    return
                if path == "/api/exo":
                    self._send(200, json.dumps(app.exo.snapshot()))
                    return
                if path == "/api/exo/prefill":
                    self._send(200, json.dumps(app.exo.prefill()))
                    return
                if path == "/api/expedition":
                    self._send(200, json.dumps(app.expedition.snapshot()))
                    return
                if path == "/api/guardian":
                    self._send(200, json.dumps(app.guardian.snapshot()))
                    return
                if path == "/api/system":
                    self._send(200, json.dumps(app.system.snapshot()))
                    return
                if path == "/api/mining":
                    self._send(200, json.dumps(app.mining.snapshot()))
                    return
                if path == "/api/engmat":
                    self._send(200, json.dumps(app.engmat.snapshot()))
                    return
                if path == "/api/cg":
                    self._send(200, json.dumps(app.cg.snapshot()))
                    return
                if path == "/api/colony":
                    self._send(200, json.dumps(app.colony.snapshot()))
                    return
                if path == "/api/engmat/lookup":
                    self._send(200, json.dumps(app.engmat.lookup((query.get("q") or [""])[0])))
                    return
                if path == "/api/shipwright":
                    self._send(200, json.dumps(app.shipwright.snapshot()))
                    return
                if path == "/api/edd":
                    self._send(200, json.dumps(app.edd.snapshot()))
                    return
                if path == "/api/presets":
                    presets = []
                    pdir = os.path.join(WEB_DIR, "presets")
                    try:
                        for fn in sorted(os.listdir(pdir)):
                            if not fn.endswith(".json"):
                                continue
                            try:
                                with open(os.path.join(pdir, fn), encoding="utf-8") as fh:
                                    name = (json.load(fh) or {}).get("name") or fn[:-5]
                                presets.append({"slug": fn[:-5], "name": name})
                            except (OSError, ValueError):
                                pass
                    except OSError:
                        pass
                    self._send(200, json.dumps(presets))
                    return
                if path == "/api/catalog/ships":
                    self._send(200, json.dumps(catalog.get().ships()))
                    return
                if path == "/api/catalog/ship":
                    self._send(200, json.dumps(catalog.get().ship((query.get("key") or [""])[0])))
                    return
                if path == "/api/catalog/modules":
                    cat = (query.get("category") or ["core"])[0]
                    self._send(200, json.dumps(catalog.get().modules_by_category(cat)))
                    return
                if path == "/api/catalog/blueprints":
                    grp = (query.get("grp") or [""])[0]
                    self._send(200, json.dumps(catalog.get().blueprints_for(grp)))
                    return
                # static files from web/
                rel = "index.html" if path in ("/", "") else path.lstrip("/")
                full = os.path.normpath(os.path.join(WEB_DIR, rel))
                if not full.startswith(WEB_DIR) or not os.path.isfile(full):
                    self._send(404, "not found", "text/plain")
                    return
                ext = os.path.splitext(full)[1].lower()
                with open(full, "rb") as handle:
                    self._send(200, handle.read(), _CONTENT_TYPES.get(ext, "application/octet-stream"))

            def do_POST(self):
                path = urlparse(self.path).path
                body = self._read_json_body()
                if path == "/api/cargo/generate":
                    app.cargo.generate(body)
                    self._send(200, json.dumps(app.cargo.snapshot()))
                    return
                if path == "/api/exo/generate":
                    app.exo.generate(body)
                    self._send(200, json.dumps(app.exo.snapshot()))
                    return
                if path == "/api/guardian/toggle":
                    app.guardian.toggle(str(body.get("key") or ""))
                    self._send(200, json.dumps(app.guardian.snapshot()))
                    return
                if path.startswith("/api/expedition/"):
                    exp, action = app.expedition, path.rsplit("/", 1)[-1]
                    try:
                        if action == "plot":
                            exp.plot(str(body.get("kind") or "neutron"), body.get("params") or body)
                        elif action == "from-exo":
                            exp.use_exo_route(str(body.get("name") or ""))
                        elif action == "from-systems":
                            exp.from_systems(str(body.get("text") or ""), str(body.get("name") or ""),
                                             str(body.get("objective") or ""))
                        elif action == "goto":
                            exp.goto(int(body.get("index") or 0))
                        elif action == "skip":
                            exp.skip(int(body.get("index") or 0))
                        elif action == "objective":
                            exp.toggle_objective(int(body.get("wp") or 0), int(body.get("obj") or 0))
                        elif action == "copy":
                            exp.copy_system(str(body.get("system") or ""))
                        elif action == "rename":
                            exp.rename(str(body.get("name") or ""))
                        elif action == "reset":
                            exp.reset()
                        elif action == "save":
                            exp.save(body.get("name"))
                        elif action == "load":
                            if not exp.load(str(body.get("slug") or "")):
                                self._send(404, json.dumps({"error": "expedition not found"}))
                                return
                        elif action == "delete":
                            exp.delete(str(body.get("slug") or ""))
                        else:
                            self._send(404, json.dumps({"error": "not found"}))
                            return
                        self._send(200, json.dumps(exp.snapshot()))
                    except Exception as exc:  # noqa: BLE001
                        self._send(400, json.dumps({"error": str(exc)}))
                    return
                if path == "/api/guardian/broker":
                    app.guardian.find_broker()
                    self._send(200, json.dumps(app.guardian.snapshot()))
                    return
                if path == "/api/mining/reset":
                    app.mining.reset_session()
                    self._send(200, json.dumps(app.mining.snapshot()))
                    return
                if path == "/api/mining/sell":
                    app.mining.find_sales(str(body.get("commodity") or ""))
                    self._send(200, json.dumps(app.mining.snapshot()))
                    return
                if path == "/api/engmat/traders":
                    app.engmat.find_traders()
                    self._send(200, json.dumps(app.engmat.snapshot()))
                    return
                if path == "/api/cg/inara-key":
                    app.cg.set_inara_key(str(body.get("key") or ""))
                    self._send(200, json.dumps(app.cg.snapshot()))
                    return
                if path == "/api/colony/source":
                    app.colony.source(str(body.get("commodity") or ""))
                    self._send(200, json.dumps(app.colony.snapshot()))
                    return
                if path == "/api/colony/candidates":
                    app.colony.find_candidates(float(body.get("radius") or 40))
                    self._send(200, json.dumps(app.colony.snapshot()))
                    return
                if path == "/api/guardian/market":
                    app.guardian.find_market(str(body.get("commodity") or ""))
                    self._send(200, json.dumps(app.guardian.snapshot()))
                    return
                if path == "/api/cargo/goto":
                    app.cargo.goto_stop(int(body.get("index") or 0))
                    self._send(200, json.dumps(app.cargo.snapshot()))
                    return
                if path == "/api/cargo/skip":
                    app.cargo.toggle_skip(int(body.get("hop")))
                    self._send(200, json.dumps(app.cargo.snapshot()))
                    return
                if path == "/api/cargo/copy":
                    ok = app.cargo.copy_system(str(body.get("system") or ""))
                    self._send(200, json.dumps({"ok": ok}))
                    return
                if path == "/api/cargo/reset":
                    app.cargo.reset()
                    self._send(200, json.dumps(app.cargo.snapshot()))
                    return
                if path == "/api/nav/threshold":
                    app.nav.set_threshold(int(body.get("threshold") or 0))
                    self._send(200, json.dumps(app.nav.snapshot()))
                    return
                if path == "/api/shipwright/build":
                    try:
                        app.shipwright.load_build(body)
                        self._send(200, json.dumps(app.shipwright.snapshot()))
                    except Exception as exc:  # noqa: BLE001
                        self._send(400, json.dumps({"error": str(exc)}))
                    return
                if path == "/api/shipwright/new":
                    app.shipwright.new_build(body.get("ship") or None)
                    self._send(200, json.dumps(app.shipwright.snapshot()))
                    return
                if path == "/api/shipwright/module":
                    if body.get("action") == "remove":
                        app.shipwright.remove_module(int(body.get("index")))
                    else:
                        app.shipwright.edit_module(int(body.get("index", -1)), body.get("patch") or {})
                    self._send(200, json.dumps(app.shipwright.snapshot()))
                    return
                if path == "/api/shipwright/save":
                    try:
                        saved = app.shipwright.save_build(body.get("name"))
                        self._send(200, json.dumps({"saved": saved, **app.shipwright.snapshot()}))
                    except Exception as exc:  # noqa: BLE001
                        self._send(400, json.dumps({"error": str(exc)}))
                    return
                if path == "/api/shipwright/load":
                    if app.shipwright.load_saved(body.get("slug") or "") is None:
                        self._send(404, json.dumps({"error": "build not found"}))
                    else:
                        self._send(200, json.dumps(app.shipwright.snapshot()))
                    return
                if path == "/api/shipwright/delete":
                    app.shipwright.delete_build(body.get("slug") or "")
                    self._send(200, json.dumps(app.shipwright.snapshot()))
                    return
                if path == "/api/shipwright/acquire":
                    try:
                        app.shipwright.plan_acquisition()
                        self._send(200, json.dumps(app.shipwright.snapshot()))
                    except Exception as exc:  # noqa: BLE001
                        self._send(400, json.dumps({"error": str(exc)}))
                    return
                self._send(404, json.dumps({"error": "not found"}))

        httpd = ThreadingHTTPServer((host, port), Handler)
        shown = "localhost" if host in ("0.0.0.0", "") else host
        print(f"EliteOps dashboard: http://{shown}:{port}  (LAN devices: http://<this-pc-ip>:{port})")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self.store.save()  # best-effort save on graceful shutdown
            httpd.server_close()


def main() -> None:
    EliteOps().serve()


if __name__ == "__main__":
    main()
