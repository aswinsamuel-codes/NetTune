"""Fixes that flip one advanced property of the Wi-Fi driver.

Driver vendors name these properties differently, so each fix finds its property
by a name pattern and picks the target value from an ordered list of patterns.
"""
from __future__ import annotations

import re

from ..system import as_list, run_ps, run_ps_json
from .base import OFF, ON, UNSUPPORTED, Context, Fix, FixError, status

_PS_READ = r"""
$a = Get-NetAdapter -InterfaceIndex ([int]$env:NT_IFINDEX)
$p = Get-NetAdapterAdvancedProperty -Name $a.Name -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -match $env:NT_PATTERN } | Select-Object -First 1
if (-not $p) { '{"found":false}'; return }
[pscustomobject]@{
    found       = $true
    displayName = [string]$p.DisplayName
    value       = [string]$p.DisplayValue
    valid       = @($p.ValidDisplayValues)
} | ConvertTo-Json
"""

_PS_WRITE = r"""
$a = Get-NetAdapter -InterfaceIndex ([int]$env:NT_IFINDEX)
Set-NetAdapterAdvancedProperty -Name $a.Name -DisplayName $env:NT_DISPLAYNAME -DisplayValue $env:NT_VALUE
"""


def pick_value(valid: list[str], preferences: list[str]):
    """First valid value matching the earliest preference pattern."""
    for pattern in preferences:
        for value in valid:
            if re.search(pattern, value, re.I):
                return value
    return None


class AdapterPropertyFix(Fix):
    name_pattern = ""
    value_preferences: list[str] = []
    category = "adapter"
    drops_connection = True

    def _read(self, ctx: Context) -> dict:
        data = run_ps_json(_PS_READ, {"ifindex": ctx.if_index, "pattern": self.name_pattern}) or {}
        if not data.get("found"):
            return {"found": False}
        valid = [str(v) for v in as_list(data.get("valid"))]
        return {
            "found": True,
            "display_name": data.get("displayName"),
            "value": data.get("value"),
            "valid": valid,
            "target": pick_value(valid, self.value_preferences),
        }

    def status(self, ctx: Context) -> dict:
        if not ctx.is_wifi:
            return status(UNSUPPORTED, "Only applies to Wi-Fi connections.")
        prop = self._read(ctx)
        if not prop["found"] or not prop["target"]:
            return status(UNSUPPORTED, "This Wi-Fi driver does not offer the setting.")
        detail = f"{prop['display_name']}: {prop['value']}"
        return status(ON if prop["value"] == prop["target"] else OFF, detail, target=prop["target"])

    def apply(self, ctx: Context, params: dict) -> str:
        prop = self._read(ctx)
        if not prop["found"] or not prop["target"]:
            raise FixError("This Wi-Fi driver does not offer the setting.")
        self.remember(ctx, {"display_name": prop["display_name"], "value": prop["value"]})
        run_ps(_PS_WRITE, {"ifindex": ctx.if_index, "displayname": prop["display_name"], "value": prop["target"]})
        return f"{prop['display_name']} set to {prop['target']}."

    def revert(self, ctx: Context) -> str:
        original = (self.saved(ctx) or {}).get("original")
        if not original:
            raise FixError("NetTune has no saved value for this setting, so there is nothing to restore.")
        run_ps(_PS_WRITE, {"ifindex": ctx.if_index, "displayname": original["display_name"], "value": original["value"]})
        self.forget(ctx)
        return f"{original['display_name']} restored to {original['value']}."


class Prefer5GhzFix(AdapterPropertyFix):
    id = "prefer_5ghz"
    title = "Prefer the 5 GHz band"
    summary = "Tells the Wi-Fi driver to pick 5 GHz over 2.4 GHz when a network offers both."
    details = (
        "2.4 GHz is crowded and slow; 5 GHz is much faster at short to medium range. "
        "Trade-off: far from the access point 5 GHz is weaker, though the driver still "
        "falls back to 2.4 GHz when it has to. The Wi-Fi link restarts for a few seconds."
    )
    name_pattern = r"prefer.*band|band.*prefer"
    value_preferences = [r"(?<![\d.])5\s*G"]


class RoamingFix(AdapterPropertyFix):
    id = "roaming"
    title = "Roam to closer access points sooner"
    summary = "Raises the driver's roaming aggressiveness so it stops clinging to a far-away access point."
    details = (
        "A campus has many access points under one name. With default roaming the laptop "
        "holds on to the first one even after you walk away from it. Trade-off: brief "
        "hiccups when it switches; not useful on a single-router network or a hotspot. "
        "The Wi-Fi link restarts for a few seconds."
    )
    name_pattern = r"roam"
    value_preferences = [r"medium[\s-]*high", r"^(?!.*highest).*high", r"aggressive", r"highest"]
