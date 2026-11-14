"""The engine: everything the dashboard can ask for, behind one ``handle`` call."""
from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from pathlib import Path

from . import __version__, advisor, background, combine, dnsbench, latency, netinfo, profiles, speedtest, wifi
from .fixes import ACTIONS, FIXES, ON, Context, FixError, list_fixes, run_action
from .history import History, summarise_run
from .state import Store
from .system import CommandError, is_admin

_DENIED = re.compile(r"access is denied|requires elevation|administrator|permission", re.I)
WATCH_INTERVAL_S = 20


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "error"):
        super().__init__(message)
        self.status = status
        self.code = code


class Engine:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.history = History(self.store)
        self.combiner = combine.Combiner(self.store)
        self.port = 0
        self.on_shutdown = None  # set by the server
        self.latest: dict = {}
        self._conn = None
        self._conn_at = 0.0
        self._conn_lock = threading.Lock()
        self._speed_lock = threading.Lock()
        self._change_lock = threading.Lock()
        self._stop = threading.Event()
        self._watcher = threading.Thread(target=self._watch, daemon=True)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        # If a previous run died while the combiner owned the Windows proxy, undo that first.
        try:
            self.combiner.restore_system_proxy()
        except OSError:
            pass
        self._watcher.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.combiner.stop()
        except OSError:
            pass

    # -- shared state --------------------------------------------------------
    def connection(self, fresh: bool = False) -> dict:
        with self._conn_lock:
            if fresh or self._conn is None or time.time() - self._conn_at > 8:
                self._conn = netinfo.get_connection()
                self._conn_at = time.time()
            return self._conn

    def context(self, fresh: bool = False) -> Context:
        return Context(conn=self.connection(fresh), store=self.store)

    def _require_connection(self) -> dict:
        conn = self.connection()
        if not conn.get("connected"):
            raise ApiError(409, "No active network connection.", "offline")
        return conn

    # -- tests ---------------------------------------------------------------
    def status(self) -> dict:
        conn = self.connection(fresh=True)
        self.latest["connection"] = conn
        return {"version": __version__, "admin": is_admin(), "connection": conn}

    def test_latency(self) -> dict:
        result = latency.run(self._require_connection().get("gateway"))
        self.latest["latency"] = result
        return result

    def test_dns(self) -> dict:
        result = dnsbench.run(self._require_connection().get("dns") or [])
        self.latest["dns"] = result
        return result

    def test_nearby(self) -> dict:
        conn = self._require_connection()
        if not conn.get("is_wifi"):
            result = {"networks": [], "summary": {}, "skipped": "Not on Wi-Fi."}
        else:
            result = wifi.scan_networks()
            current = conn.get("wifi") if not (conn.get("wifi") or {}).get("error") else None
            result["summary"] = wifi.summarise_scan(result["networks"], current)
        self.latest["nearby"] = result
        return result

    def test_background(self) -> dict:
        result = background.run(self._require_connection()["if_index"])
        self.latest["background"] = result
        return result

    def test_speed(self, body: dict) -> dict:
        self._require_connection()
        if not self._speed_lock.acquire(blocking=False):
            raise ApiError(409, "A speed test is already running.", "busy")
        try:
            result = speedtest.run("lite" if body.get("mode") == "lite" else "full")
        finally:
            self._speed_lock.release()
        self.latest["speed"] = result
        return result

    def analyze(self, body: dict) -> dict:
        ctx = self.context()
        fixes = list_fixes(ctx)
        states = {entry["id"]: entry["status"]["state"] for entry in fixes}
        analysis = advisor.analyze(self.latest, states)
        if body.get("save", True) and self.latest.get("latency"):
            fixes_on = [e["id"] for e in fixes if e["status"]["state"] == ON and e["status"].get("managed")]
            self.history.add_run(summarise_run(self.latest, analysis, fixes_on))
        return analysis

    # -- fixes ---------------------------------------------------------------
    def fixes(self) -> dict:
        actions = [{k: v for k, v in action.items() if k != "run"} for action in ACTIONS.values()]
        return {"admin": is_admin(), "fixes": list_fixes(self.context()), "actions": actions}

    def _guarded(self, needs_admin: bool, fn):
        """Run one system change at a time and translate failures for the API."""
        if needs_admin and not is_admin():
            raise ApiError(403, "This change needs administrator rights.", "needs_admin")
        with self._change_lock:
            try:
                return fn()
            except FixError as exc:
                raise ApiError(400, str(exc), "fix_failed") from exc
            except CommandError as exc:
                if _DENIED.search(str(exc)) and not is_admin():
                    raise ApiError(403, "Windows refused the change without administrator rights.", "needs_admin") from exc
                raise ApiError(500, str(exc), "command_failed") from exc
            finally:
                self._conn = None  # the change may have altered the connection

    def change_fix(self, fix_id: str, verb: str, body: dict) -> dict:
        fix = FIXES.get(fix_id)
        if not fix:
            raise ApiError(404, "Unknown fix.", "not_found")
        ctx = self.context(fresh=True)
        if not ctx.connected:
            raise ApiError(409, "No active network connection.", "offline")
        if verb == "apply":
            message = self._guarded(fix.requires_admin, lambda: fix.apply(ctx, body.get("params") or {}))
        else:
            message = self._guarded(fix.requires_admin, lambda: fix.revert(ctx))
        self.history.add_event(message, ctx.label)
        return {"ok": True, "message": message, "status": fix.full_status(self.context(fresh=True))}

    def action(self, action_id: str) -> dict:
        action = ACTIONS.get(action_id)
        if not action:
            raise ApiError(404, "Unknown action.", "not_found")
        ctx = self.context(fresh=True)
        message = self._guarded(action["requires_admin"], lambda: run_action(action_id, ctx))
        self.history.add_event(message, ctx.label)
        return {"ok": True, "message": message}

    # -- profiles ------------------------------------------------------------
    def profile_list(self) -> dict:
        data = profiles.load(self.store)
        data["ssid"] = self.connection().get("ssid")
        data["hotspot_likely"] = bool(self.connection().get("hotspot_likely"))
        return data

    def profile_apply(self, pid: str) -> dict:
        ctx = self.context(fresh=True)
        if not ctx.connected:
            raise ApiError(409, "No active network connection.", "offline")
        outcomes = self._guarded(False, lambda: profiles.apply_profile(pid, ctx, is_admin()))
        changed = [o for o in outcomes if o["outcome"] in ("applied", "reverted")]
        name = profiles.DEFAULTS.get(pid, {}).get("name", pid)
        self.history.add_event(f"Profile '{name}' applied ({len(changed)} changes).", ctx.label)
        return {"ok": True, "outcomes": outcomes}

    def profile_edit(self, pid: str, body: dict) -> dict:
        self._guarded(False, lambda: profiles.set_fixes(self.store, pid, body.get("fixes") or []))
        return self.profile_list()

    def profile_bind(self, body: dict) -> dict:
        self._guarded(False, lambda: profiles.bind(self.store, str(body.get("ssid") or ""), body.get("profile")))
        return self.profile_list()

    def profile_auto(self, body: dict) -> dict:
        profiles.set_auto(self.store, bool(body.get("enabled")))
        return self.profile_list()

    def _watch(self) -> None:
        """Apply the bound profile when the laptop joins a known network."""
        last = None
        while not self._stop.wait(WATCH_INTERVAL_S):
            try:
                config = profiles.load(self.store)
                if not config["auto"] or not config["bindings"]:
                    last = None
                    continue
                iface = wifi.current_interface()
                ssid = iface.get("ssid") if iface.get("connected") else None
                if ssid and ssid != last and ssid in config["bindings"]:
                    if self._stop.wait(6):  # let the new connection settle
                        return
                    ctx = self.context(fresh=True)
                    if ctx.connected and ctx.conn.get("ssid") == ssid:
                        pid = config["bindings"][ssid]
                        with self._change_lock:
                            outcomes = profiles.apply_profile(pid, ctx, is_admin())
                        self._conn = None
                        changed = sum(1 for o in outcomes if o["outcome"] in ("applied", "reverted"))
                        if changed:
                            self.history.add_event(f"Auto-applied profile '{profiles.DEFAULTS[pid]['name']}' ({changed} changes).", ssid)
                last = ssid
            except Exception:
                continue  # the watcher must survive transient failures

    # -- combine -------------------------------------------------------------
    def combine_status(self) -> dict:
        try:
            return self.combiner.status()
        except combine.CombineError as exc:
            raise ApiError(500, str(exc), "combine_failed") from exc

    def combine_start(self, body: dict) -> dict:
        try:
            status = self.combiner.start(system_proxy=bool(body.get("system_proxy", True)))
        except combine.CombineError as exc:
            raise ApiError(400, str(exc), "combine_failed") from exc
        names = " + ".join(u["name"] for u in status["uplinks"])
        self.history.add_event(f"Combine started: {names}.", self.connection().get("label"))
        return status

    def combine_stop(self) -> dict:
        self.combiner.stop()
        self.history.add_event("Combine stopped.", self.connection().get("label"))
        return self.combine_status()

    # -- process control -----------------------------------------------------
    def elevate(self) -> dict:
        if is_admin():
            return {"ok": True, "already": True}
        root = str(Path(__file__).resolve().parent.parent)
        params = f"-m nettune --port {self.port} --takeover --no-browser"
        code = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, root, 1)
        if code <= 32:
            raise ApiError(400, "Administrator permission was not granted.", "declined")
        if self.on_shutdown:
            threading.Timer(0.8, self.on_shutdown).start()
        return {"ok": True}

    def shutdown(self) -> dict:
        if self.on_shutdown:
            threading.Timer(0.3, self.on_shutdown).start()
        return {"ok": True}

    # -- routing -------------------------------------------------------------
    def handle(self, method: str, path: str, body: dict) -> dict:
        route = (method, path)
        simple = {
            ("GET", "/api/status"): self.status,
            ("POST", "/api/test/latency"): self.test_latency,
            ("POST", "/api/test/dns"): self.test_dns,
            ("POST", "/api/test/nearby"): self.test_nearby,
            ("POST", "/api/test/background"): self.test_background,
            ("GET", "/api/progress"): speedtest.progress,
            ("GET", "/api/fixes"): self.fixes,
            ("GET", "/api/profiles"): self.profile_list,
            ("GET", "/api/history"): lambda: {"entries": self.history.entries()},
            ("GET", "/api/combine"): self.combine_status,
            ("POST", "/api/combine/stop"): self.combine_stop,
            ("POST", "/api/elevate"): self.elevate,
            ("POST", "/api/shutdown"): self.shutdown,
        }
        with_body = {
            ("POST", "/api/test/speed"): self.test_speed,
            ("POST", "/api/analyze"): self.analyze,
            ("POST", "/api/combine/start"): self.combine_start,
            ("POST", "/api/profiles/bind"): self.profile_bind,
            ("POST", "/api/profiles/auto"): self.profile_auto,
        }
        if route in simple:
            return simple[route]()
        if route in with_body:
            return with_body[route](body)
        if route == ("DELETE", "/api/history"):
            self.history.clear()
            return {"ok": True}
        if method == "POST":
            match = re.fullmatch(r"/api/fixes/([a-z0-9_]+)/(apply|revert)", path)
            if match:
                return self.change_fix(match.group(1), match.group(2), body)
            match = re.fullmatch(r"/api/actions/([a-z0-9_]+)", path)
            if match:
                return self.action(match.group(1))
            match = re.fullmatch(r"/api/profiles/([a-z0-9_]+)/apply", path)
            if match:
                return self.profile_apply(match.group(1))
            match = re.fullmatch(r"/api/profiles/([a-z0-9_]+)", path)
            if match:
                return self.profile_edit(match.group(1), body)
        raise ApiError(404, "Unknown endpoint.", "not_found")
