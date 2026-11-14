"""Registry of fixes and one-shot actions."""
from __future__ import annotations

import threading

from ..system import CommandError, run_cmd
from .adapter import Prefer5GhzFix, RoamingFix
from .base import ERROR, OFF, ON, UNSUPPORTED, Context, Fix, FixError
from .dns import DnsFix
from .power import PowerSavingFix
from .tweaks import DeliveryOptimizationFix, MeteredFix, TcpAutoTuningFix

FIXES: dict[str, Fix] = {
    fix.id: fix
    for fix in (
        DnsFix(),
        Prefer5GhzFix(),
        RoamingFix(),
        PowerSavingFix(),
        TcpAutoTuningFix(),
        DeliveryOptimizationFix(),
        MeteredFix(),
    )
}

__all__ = ["FIXES", "ACTIONS", "Context", "Fix", "FixError", "ON", "OFF", "UNSUPPORTED", "ERROR",
           "list_fixes", "run_action"]


def list_fixes(ctx: Context) -> list[dict]:
    """Every fix with its live status. Status checks run in parallel."""
    entries = [fix.describe() for fix in FIXES.values()]

    def worker(entry):
        entry["status"] = FIXES[entry["id"]].full_status(ctx)

    threads = [threading.Thread(target=worker, args=(entry,)) for entry in entries]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return entries


def _flush_dns(ctx: Context) -> str:
    code, text = run_cmd(["ipconfig", "/flushdns"])
    if code != 0:
        raise FixError(text.strip()[:200] or "Could not flush the DNS cache.")
    return "DNS cache flushed."


def _reconnect_wifi(ctx: Context) -> str:
    profile = ctx.wifi_profile
    if not ctx.is_wifi or not profile or not ctx.adapter:
        raise FixError("Only works on a saved Wi-Fi network.")
    if '"' in profile or '"' in ctx.adapter:
        raise FixError("This network name contains characters NetTune will not pass to netsh.")
    run_cmd(["netsh", "wlan", "disconnect", f"interface={ctx.adapter}"])
    code, text = run_cmd(["netsh", "wlan", "connect", f"name={profile}", f"interface={ctx.adapter}"])
    if code != 0:
        raise FixError("Disconnected, but reconnecting failed - reconnect from the taskbar. " + text.strip()[:160])
    return f"Reconnected to '{profile}'. Windows picks the strongest access point on a fresh connection."


ACTIONS = {
    "flush_dns": {
        "id": "flush_dns",
        "title": "Flush the DNS cache",
        "summary": "Clears stale name lookups. Useful right after changing DNS or when one site will not load.",
        "requires_admin": False,
        "drops_connection": False,
        "run": _flush_dns,
    },
    "reconnect_wifi": {
        "id": "reconnect_wifi",
        "title": "Reconnect to the strongest access point",
        "summary": "Drops and rejoins the current Wi-Fi so Windows re-picks the best access point and band.",
        "requires_admin": False,
        "drops_connection": True,
        "run": _reconnect_wifi,
    },
}


def run_action(action_id: str, ctx: Context) -> str:
    action = ACTIONS.get(action_id)
    if not action:
        raise FixError("Unknown action.")
    try:
        return action["run"](ctx)
    except CommandError as exc:
        raise FixError(str(exc)) from exc
