"""Profiles: named sets of fixes, optionally bound to a Wi-Fi network name."""
from __future__ import annotations

from .fixes import FIXES, OFF, ON, Context, FixError
from .state import Store
from .system import CommandError

DEFAULTS = {
    "college": {
        "name": "College Wi-Fi",
        "description": "Many access points, shared bandwidth: favour 5 GHz, roam sooner, use the fastest DNS.",
        "fixes": ["power_saving", "prefer_5ghz", "roaming", "dns", "tcp_autotuning", "delivery_optimization"],
    },
    "hotspot": {
        "name": "Phone hotspot",
        "description": "One access point, limited mobile data: favour 5 GHz and stop background downloads.",
        "fixes": ["power_saving", "prefer_5ghz", "metered", "tcp_autotuning", "delivery_optimization"],
    },
}


def load(store: Store) -> dict:
    config = store.get("profiles", {})
    overrides = config.get("fixes", {})
    profiles = {}
    for pid, base in DEFAULTS.items():
        fixes = [f for f in overrides.get(pid, base["fixes"]) if f in FIXES]
        profiles[pid] = {"id": pid, "name": base["name"], "description": base["description"], "fixes": fixes}
    return {
        "profiles": list(profiles.values()),
        "bindings": config.get("bindings", {}),
        "auto": bool(config.get("auto")),
    }


def set_fixes(store: Store, pid: str, fix_ids: list) -> None:
    if pid not in DEFAULTS:
        raise FixError("Unknown profile.")
    chosen = [f for f in FIXES if f in set(fix_ids or [])]

    def update(config):
        config.setdefault("fixes", {})[pid] = chosen
        return config

    store.update("profiles", {}, update)


def bind(store: Store, ssid: str, pid: str | None) -> None:
    if not ssid or len(ssid) > 64:
        raise FixError("A network name is required.")
    if pid is not None and pid not in DEFAULTS:
        raise FixError("Unknown profile.")

    def update(config):
        bindings = config.setdefault("bindings", {})
        if pid is None:
            bindings.pop(ssid, None)
        else:
            bindings[ssid] = pid
        return config

    store.update("profiles", {}, update)


def set_auto(store: Store, enabled: bool) -> None:
    def update(config):
        config["auto"] = bool(enabled)
        return config

    store.update("profiles", {}, update)


def apply_profile(pid: str, ctx: Context, admin: bool) -> list[dict]:
    """Turn on the profile's fixes; revert fixes NetTune applied that it leaves out."""
    profile = next((p for p in load(ctx.store)["profiles"] if p["id"] == pid), None)
    if not profile:
        raise FixError("Unknown profile.")
    wanted = set(profile["fixes"])
    outcomes = []
    for fix_id, fix in FIXES.items():
        current = fix.full_status(ctx)
        entry = {"fix": fix_id, "title": fix.title, "outcome": "unchanged", "message": current.get("detail", "")}
        try:
            if fix_id in wanted:
                if current["state"] == ON:
                    entry["outcome"] = "already"
                elif current["state"] != OFF:
                    entry["outcome"] = "skipped"
                elif fix.requires_admin and not admin:
                    entry.update(outcome="skipped", message="Needs administrator rights.")
                else:
                    params = {"provider": "auto", "only_if_faster": True} if fix_id == "dns" else {}
                    entry.update(outcome="applied", message=fix.apply(ctx, params))
            elif current.get("managed") and current["state"] == ON:
                if fix.requires_admin and not admin:
                    entry.update(outcome="skipped", message="Needs administrator rights to revert.")
                else:
                    entry.update(outcome="reverted", message=fix.revert(ctx))
            else:
                continue
        except (FixError, CommandError) as exc:
            entry.update(outcome="failed", message=str(exc))
        outcomes.append(entry)
    return outcomes
