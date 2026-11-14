"""Which adapter carries the internet connection, and what it looks like."""
from __future__ import annotations

import re
from datetime import date

from . import wifi
from .system import CommandError, as_list, run_ps_json

_PS_CONNECTION = r"""
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1
if (-not $route) { '{"connected":false}'; return }
$i = $route.ifIndex
$a = Get-NetAdapter -InterfaceIndex $i
$ip = Get-NetIPAddress -InterfaceIndex $i -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
$dns = Get-DnsClientServerAddress -InterfaceIndex $i -AddressFamily IPv4 -ErrorAction SilentlyContinue
$prof = Get-NetConnectionProfile -InterfaceIndex $i -ErrorAction SilentlyContinue | Select-Object -First 1
[pscustomobject]@{
    connected      = $true
    ifIndex        = [int]$i
    name           = $a.Name
    description    = $a.InterfaceDescription
    guid           = $a.InterfaceGuid
    isWifi         = [bool]($a.PhysicalMediaType -match '802\.11')
    linkSpeed      = [string]$a.LinkSpeed
    driverVersion  = [string]$a.DriverVersion
    driverDate     = [string]$a.DriverDate
    driverProvider = [string]$a.DriverProvider
    gateway        = [string]$route.NextHop
    ip             = [string]$ip.IPAddress
    dns            = @($dns.ServerAddresses)
    profileName    = [string]$prof.Name
} | ConvertTo-Json -Depth 4
"""

_PHONE_SSID = re.compile(
    r"iphone|android|galaxy|redmi|xiaomi|poco|oneplus|vivo|oppo|realme|pixel|moto|nothing|"
    r"infinix|tecno|iqoo|hotspot",
    re.I,
)
_PHONE_GATEWAYS = ("192.168.43.1", "172.20.10.1")


def _driver_age_days(text: str | None):
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if not match:
        return None
    try:
        return (date.today() - date(*map(int, match.groups()))).days
    except ValueError:
        return None


def looks_like_hotspot(gateway: str | None, ssid: str | None) -> bool:
    """Best-effort guess used only to word hints; never to change settings."""
    if gateway in _PHONE_GATEWAYS:
        return True
    return bool(ssid and _PHONE_SSID.search(ssid))


def get_connection() -> dict:
    try:
        data = run_ps_json(_PS_CONNECTION)
    except CommandError as exc:
        return {"connected": False, "error": str(exc)}
    if not data or not data.get("connected"):
        return {"connected": False}

    gateway = data.get("gateway") or None
    if gateway in ("0.0.0.0", "::"):
        gateway = None  # on-link default route, typical for VPNs
    conn = {
        "connected": True,
        "if_index": data["ifIndex"],
        "adapter": data.get("name"),
        "description": data.get("description"),
        "guid": data.get("guid"),
        "is_wifi": bool(data.get("isWifi")),
        "link_speed": data.get("linkSpeed"),
        "driver_version": data.get("driverVersion") or None,
        "driver_date": (data.get("driverDate") or "")[:10] or None,
        "driver_provider": data.get("driverProvider") or None,
        "driver_age_days": _driver_age_days(data.get("driverDate")),
        "gateway": gateway,
        "ip": data.get("ip") or None,
        "dns": [d for d in as_list(data.get("dns")) if d],
        "network_name": data.get("profileName") or None,
        "wifi": None,
    }
    if conn["is_wifi"]:
        conn["wifi"] = wifi.current_interface(conn["adapter"])
    # "ssid" is the real Wi-Fi name (None when Windows hides it); "label" is
    # what to show and file history under. The Windows network name is not
    # always the SSID, so it must never be used to address a Wi-Fi profile.
    conn["ssid"] = (conn["wifi"] or {}).get("ssid") or None
    conn["label"] = conn["ssid"] or conn["network_name"] or conn["adapter"]
    conn["link_mbps"] = parse_link_speed(conn["link_speed"])
    conn["hotspot_likely"] = looks_like_hotspot(gateway, conn["label"])
    return conn


def parse_link_speed(text: str | None):
    """'866.7 Mbps' / '1 Gbps' -> Mbps as a float."""
    match = re.match(r"\s*([\d.]+)\s*([KMG])bps", text or "", re.I)
    if not match:
        return None
    scale = {"k": 0.001, "m": 1.0, "g": 1000.0}[match.group(2).lower()]
    return round(float(match.group(1)) * scale, 1)
