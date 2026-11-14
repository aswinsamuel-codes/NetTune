"""Wi-Fi information from ``netsh wlan``.

The parsers are pure functions over text so they can be unit-tested without a
Wi-Fi card. Known limitation: ``netsh`` output is localised and these parsers
expect the English field names.
"""
from __future__ import annotations

import re

from .system import CommandError, run_netsh_utf8

_KV = re.compile(r"^\s*([^:]+?)\s*:(?:\s(.*))?$")

GENERATIONS = {
    "802.11be": "Wi-Fi 7",
    "802.11ax": "Wi-Fi 6",
    "802.11ac": "Wi-Fi 5",
    "802.11n": "Wi-Fi 4",
    "802.11g": "802.11g",
    "802.11a": "802.11a",
    "802.11b": "802.11b",
}


def _num(text):
    match = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(match.group()) if match else None


def _int(text):
    value = _num(text)
    return int(value) if value is not None else None


def normalise_band(text, channel=None):
    """Return '2.4', '5' or '6' (GHz) or None."""
    if text:
        match = re.search(r"(2\.4|5|6)\s*GHz", text, re.I)
        if match:
            return match.group(1)
    if channel is not None:
        if 1 <= channel <= 14:
            return "2.4"
        if 32 <= channel <= 177:
            return "5"
    return None


def parse_interfaces(text: str) -> list[dict]:
    """Parse ``netsh wlan show interfaces``."""
    blocks: list[dict] = []
    current = None
    for line in text.splitlines():
        match = _KV.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = (match.group(2) or "").strip()
        if key == "name" or current is None:
            current = {}
            blocks.append(current)
        current.setdefault(key, value)

    result = []
    for block in blocks:
        if len(block) < 3:
            continue  # a header line such as "There is 1 interface on the system:"
        channel = _int(block.get("channel"))
        signal = _int(block.get("signal"))
        rssi = _int(block.get("rssi"))
        if rssi is None and signal is not None:
            rssi = int(signal / 2 - 100)
        radio = block.get("radio type")
        result.append({
            "name": block.get("name"),
            "description": block.get("description"),
            "state": block.get("state"),
            "connected": (block.get("state") or "").lower() == "connected",
            "ssid": block.get("ssid") or None,
            "bssid": (block.get("ap bssid") or block.get("bssid") or "").lower() or None,
            "radio": radio,
            "generation": GENERATIONS.get((radio or "").lower()),
            "auth": block.get("authentication"),
            "channel": channel,
            "band": normalise_band(block.get("band"), channel),
            "rx_mbps": _num(block.get("receive rate (mbps)")),
            "tx_mbps": _num(block.get("transmit rate (mbps)")),
            "signal": signal,
            "rssi": rssi,
            "profile": block.get("profile") or None,
        })
    return result


def parse_networks(text: str) -> list[dict]:
    """Parse ``netsh wlan show networks mode=bssid`` into one entry per access point."""
    networks: list[dict] = []
    ssid = None
    current = None
    for line in text.splitlines():
        match = _KV.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = (match.group(2) or "").strip()
        if re.fullmatch(r"ssid \d+", key):
            ssid = {"ssid": value, "auth": None}
            current = None
            continue
        if ssid is None:
            continue
        if re.fullmatch(r"bssid \d+", key):
            current = {
                "ssid": ssid["ssid"],
                "bssid": value.lower(),
                "auth": ssid["auth"],
                "signal": None,
                "radio": None,
                "band": None,
                "channel": None,
                "utilization": None,
                "stations": None,
            }
            networks.append(current)
            continue
        if current is None:
            if key == "authentication":
                ssid["auth"] = value
            continue
        if key == "signal":
            current["signal"] = _int(value)
        elif key == "radio type":
            current["radio"] = value
        elif key == "band":
            current["band"] = normalise_band(value)
        elif key == "channel":
            current["channel"] = _int(value)
        elif key == "channel utilization":
            pct = re.search(r"\((\d+)\s*%\)", value)
            current["utilization"] = int(pct.group(1)) if pct else None
        elif key == "connected stations":
            current["stations"] = _int(value)
    for net in networks:
        if net["band"] is None:
            net["band"] = normalise_band(None, net["channel"])
    return networks


def _location_blocked(code: int, text: str) -> bool:
    return code != 0 and "location" in text.lower()


LOCATION_HELP = (
    "Windows hides Wi-Fi details until Location services are on. Open Settings > "
    "Privacy & security > Location, turn on 'Location services' and 'Let desktop "
    "apps access your location', then run the diagnosis again."
)


def current_interface(adapter_name: str | None = None) -> dict:
    """Details of the connected Wi-Fi interface, or ``{'error': ...}``."""
    try:
        code, text = run_netsh_utf8("wlan show interfaces")
    except CommandError as exc:
        return {"error": "unavailable", "message": str(exc)}
    if _location_blocked(code, text):
        return {"error": "location", "message": LOCATION_HELP}
    if code != 0:
        return {"error": "unavailable", "message": text.strip()[:300]}
    interfaces = parse_interfaces(text)
    if adapter_name:
        for iface in interfaces:
            if iface["name"] == adapter_name:
                return iface
    for iface in interfaces:
        if iface["connected"]:
            return iface
    return interfaces[0] if interfaces else {"error": "unavailable", "message": "No Wi-Fi interface found"}


def scan_networks() -> dict:
    try:
        code, text = run_netsh_utf8("wlan show networks mode=bssid")
    except CommandError as exc:
        return {"error": "unavailable", "message": str(exc), "networks": []}
    if _location_blocked(code, text):
        return {"error": "location", "message": LOCATION_HELP, "networks": []}
    if code != 0:
        return {"error": "unavailable", "message": text.strip()[:300], "networks": []}
    return {"networks": parse_networks(text)}


def summarise_scan(networks: list[dict], wifi: dict | None) -> dict:
    """What the neighbourhood means for the connection we are on."""
    summary = {
        "total": len(networks),
        "same_channel": None,
        "better_ap": None,
        "has_5ghz_same_ssid": False,
        "has_6ghz_same_ssid": False,
    }
    if not wifi or not wifi.get("ssid"):
        return summary
    ssid, bssid = wifi["ssid"], wifi.get("bssid")
    channel, band = wifi.get("channel"), wifi.get("band")
    if channel is not None:
        def overlaps(net):
            if net.get("bssid") == bssid or net.get("channel") is None or net.get("band") != band:
                return False
            # 2.4 GHz channels are 5 MHz apart but 20 MHz wide, so neighbours overlap.
            spread = 4 if band == "2.4" else 0
            return abs(net["channel"] - channel) <= spread
        summary["same_channel"] = sum(1 for net in networks if overlaps(net))
    mine = wifi.get("signal") or 0
    for net in networks:
        if net["ssid"] != ssid or net.get("bssid") == bssid:
            continue
        if net.get("band") == "5":
            summary["has_5ghz_same_ssid"] = True
        if net.get("band") == "6":
            summary["has_6ghz_same_ssid"] = True
        signal = net.get("signal") or 0
        best = summary["better_ap"]
        if signal >= mine + 15 and (best is None or signal > best["signal"]):
            summary["better_ap"] = {
                "bssid": net["bssid"],
                "signal": signal,
                "band": net.get("band"),
                "channel": net.get("channel"),
            }
    return summary
