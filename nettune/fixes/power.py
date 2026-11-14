"""Stop Windows from throttling the Wi-Fi radio to save power."""
from __future__ import annotations

import re

from ..system import run_cmd
from .base import OFF, ON, UNSUPPORTED, Context, Fix, FixError, status

_SUBGROUP = "19cbb8fa-5279-450e-9fac-8a3d5fedd0c1"  # Wireless Adapter Settings
_SETTING = "12bbebe6-58d6-4636-95bb-3217ef867c1a"   # Power Saving Mode
_LEVELS = {0: "Maximum performance", 1: "Low power saving", 2: "Medium power saving", 3: "Maximum power saving"}


def parse_power_query(text: str):
    """(ac, dc) indexes from ``powercfg /query`` output, or None."""
    values = re.findall(r"0x([0-9a-fA-F]{8})", text)
    if len(values) < 2:
        return None
    return int(values[-2], 16), int(values[-1], 16)


class PowerSavingFix(Fix):
    id = "power_saving"
    title = "Turn off Wi-Fi power saving"
    summary = "Sets the wireless adapter to 'Maximum performance' in the current power plan."
    details = (
        "On battery, Windows lets the Wi-Fi radio doze between packets, which adds lag and "
        "lowers throughput. Trade-off: slightly higher battery drain while Wi-Fi is busy."
    )
    category = "adapter"
    requires_admin = False

    def scope(self, ctx: Context) -> str:
        return "power-plan"

    def _read(self):
        code, text = run_cmd(["powercfg", "/query", "SCHEME_CURRENT", _SUBGROUP, _SETTING])
        return parse_power_query(text) if code == 0 else None

    def _write(self, ac: int, dc: int) -> None:
        for flag, value in (("/setacvalueindex", ac), ("/setdcvalueindex", dc)):
            code, text = run_cmd(["powercfg", flag, "SCHEME_CURRENT", _SUBGROUP, _SETTING, str(value)])
            if code != 0:
                raise FixError("powercfg refused the change: " + text.strip()[:200])
        run_cmd(["powercfg", "/setactive", "SCHEME_CURRENT"])

    def status(self, ctx: Context) -> dict:
        if not ctx.is_wifi:
            return status(UNSUPPORTED, "Only applies to Wi-Fi connections.")
        current = self._read()
        if current is None:
            return status(UNSUPPORTED, "This power plan does not expose the wireless power setting.")
        ac, dc = current
        detail = f"Plugged in: {_LEVELS.get(ac, ac)}. On battery: {_LEVELS.get(dc, dc)}."
        return status(ON if ac == 0 and dc == 0 else OFF, detail)

    def apply(self, ctx: Context, params: dict) -> str:
        current = self._read()
        if current is None:
            raise FixError("This power plan does not expose the wireless power setting.")
        self.remember(ctx, {"ac": current[0], "dc": current[1]})
        self._write(0, 0)
        return "Wi-Fi power saving is off (maximum performance)."

    def revert(self, ctx: Context) -> str:
        original = (self.saved(ctx) or {}).get("original") or {"ac": 0, "dc": 2}
        self._write(int(original.get("ac", 0)), int(original.get("dc", 2)))
        self.forget(ctx)
        return "Wi-Fi power saving restored to its previous setting."
