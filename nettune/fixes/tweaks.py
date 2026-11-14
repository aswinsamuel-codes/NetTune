"""System-level fixes: TCP auto-tuning, update sharing, metered networks."""
from __future__ import annotations

import re

from ..system import run_cmd, run_ps
from .base import OFF, ON, UNSUPPORTED, Context, Fix, FixError, status

_AUTOTUNE_LEVELS = {"disabled", "highlyrestricted", "restricted", "normal", "experimental"}


class TcpAutoTuningFix(Fix):
    id = "tcp_autotuning"
    title = "Restore TCP auto-tuning"
    summary = "Puts the TCP receive window back on Windows' default 'normal' auto-tuning."
    details = (
        "Old 'internet booster' tools and some guides disable receive-window auto-tuning, "
        "which caps download speed on fast links. This restores the Windows default; if "
        "it is already 'normal' there is nothing to do."
    )
    category = "system"

    def scope(self, ctx: Context) -> str:
        return "system"

    def _read(self) -> str:
        return run_ps("(Get-NetTCPSetting -SettingName Internet).AutoTuningLevelLocal.ToString()").strip().lower()

    def _write(self, level: str) -> None:
        code, text = run_cmd(["netsh", "interface", "tcp", "set", "global", f"autotuninglevel={level}"])
        if code != 0:
            raise FixError("netsh refused the change: " + text.strip()[:200])

    def status(self, ctx: Context) -> dict:
        level = self._read()
        if level == "normal":
            return status(ON, "Auto-tuning is at the Windows default (normal).")
        return status(OFF, f"Auto-tuning is '{level}', which can cap download speed.")

    def apply(self, ctx: Context, params: dict) -> str:
        self.remember(ctx, self._read())
        self._write("normal")
        return "TCP auto-tuning set to normal."

    def revert(self, ctx: Context) -> str:
        original = (self.saved(ctx) or {}).get("original")
        if original not in _AUTOTUNE_LEVELS:
            raise FixError("NetTune has no saved value for this setting, so there is nothing to restore.")
        self._write(original)
        self.forget(ctx)
        return f"TCP auto-tuning restored to {original}."


_DO_KEY = r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization"

_PS_DO_READ = (
    f"$v = (Get-ItemProperty -Path '{_DO_KEY}' -Name DODownloadMode -ErrorAction SilentlyContinue).DODownloadMode\n"
    "if ($null -eq $v) { 'none' } else { [string]$v }"
)
_PS_DO_SET = (
    f"$p = '{_DO_KEY}'\n"
    "if (-not (Test-Path $p)) { New-Item -Path $p -Force | Out-Null }\n"
    "Set-ItemProperty -Path $p -Name DODownloadMode -Value ([int]$env:NT_MODE) -Type DWord"
)
_PS_DO_CLEAR = f"Remove-ItemProperty -Path '{_DO_KEY}' -Name DODownloadMode -ErrorAction SilentlyContinue"


class DeliveryOptimizationFix(Fix):
    id = "delivery_optimization"
    title = "Stop sharing Windows updates with other PCs"
    summary = "Turns off Delivery Optimization peer-to-peer, which uploads update files in the background."
    details = (
        "By default Windows can upload pieces of updates to other PCs, quietly using your "
        "upload bandwidth and hotspot data. This sets the Delivery Optimization policy to "
        "'HTTP only'. Trade-off: Windows Settings will show the option as managed by policy "
        "until you revert."
    )
    category = "data"

    def scope(self, ctx: Context) -> str:
        return "system"

    def _read(self):
        value = run_ps(_PS_DO_READ).strip()
        return None if value == "none" else int(value)

    def status(self, ctx: Context) -> dict:
        mode = self._read()
        if mode == 0:
            return status(ON, "Peer-to-peer update sharing is off.")
        if mode is None:
            return status(OFF, "Windows default: updates may be shared with other PCs.")
        return status(OFF, f"A Delivery Optimization policy is set (mode {mode}).")

    def apply(self, ctx: Context, params: dict) -> str:
        self.remember(ctx, self._read())
        run_ps(_PS_DO_SET, {"mode": 0})
        return "Peer-to-peer update sharing turned off."

    def revert(self, ctx: Context) -> str:
        saved = self.saved(ctx)
        original = (saved or {}).get("original")
        if isinstance(original, int):
            run_ps(_PS_DO_SET, {"mode": original})
        else:
            run_ps(_PS_DO_CLEAR)
        self.forget(ctx)
        return "Delivery Optimization restored to its previous setting."


def parse_profile_cost(text: str):
    match = re.search(r"^\s*Cost\s*:\s*(\w+)", text, re.I | re.M)
    return match.group(1).capitalize() if match else None


class MeteredFix(Fix):
    id = "metered"
    title = "Treat this network as metered"
    summary = "Marks the current Wi-Fi network as metered so Windows and apps hold back background downloads."
    details = (
        "On a phone hotspot, Windows Update, OneDrive and the Store can eat your mobile "
        "data and your speed. A metered network makes them wait. Applies only to the "
        "network you are on now. Trade-off: updates and cloud sync pause on this network."
    )
    category = "data"
    requires_admin = False

    def scope(self, ctx: Context) -> str:
        return ctx.wifi_profile or "-"

    def _profile(self, ctx: Context) -> str:
        profile = ctx.wifi_profile
        if not ctx.is_wifi or not profile:
            raise FixError("Only applies to a saved Wi-Fi network.")
        if '"' in profile:
            raise FixError("This network name contains characters NetTune will not pass to netsh.")
        return profile

    def _read(self, profile: str):
        code, text = run_cmd(["netsh", "wlan", "show", "profile", f"name={profile}"])
        return parse_profile_cost(text) if code == 0 else None

    def _write(self, profile: str, cost: str) -> None:
        code, text = run_cmd(["netsh", "wlan", "set", "profileparameter", f"name={profile}", f"cost={cost}"])
        if code != 0:
            raise FixError("netsh refused the change: " + text.strip()[:200])

    def status(self, ctx: Context) -> dict:
        if not ctx.is_wifi:
            return status(UNSUPPORTED, "Only applies to a saved Wi-Fi network.")
        if not ctx.wifi_profile:
            return status(UNSUPPORTED, "Windows is hiding the Wi-Fi network name. Turn on Location services to use this fix.")
        profile = self._profile(ctx)
        cost = self._read(profile)
        if cost is None:
            return status(UNSUPPORTED, f"Could not read the saved profile for '{profile}'.")
        if cost in ("Fixed", "Variable"):
            return status(ON, f"'{profile}' is metered.")
        return status(OFF, f"'{profile}' is not metered.")

    def apply(self, ctx: Context, params: dict) -> str:
        profile = self._profile(ctx)
        self.remember(ctx, self._read(profile) or "Unrestricted")
        self._write(profile, "Fixed")
        return f"'{profile}' is now treated as metered."

    def revert(self, ctx: Context) -> str:
        profile = self._profile(ctx)
        original = (self.saved(ctx) or {}).get("original")
        if original not in ("Unrestricted", "Fixed", "Variable"):
            original = "Unrestricted"
        self._write(profile, original)
        self.forget(ctx)
        return f"'{profile}' restored to {original.lower()}."
