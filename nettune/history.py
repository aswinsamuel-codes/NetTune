"""Run history, so a change can be judged by before/after numbers."""
from __future__ import annotations

import time

from .state import Store

MAX_ENTRIES = 200


def summarise_run(results: dict, analysis: dict, fixes_on: list[str]) -> dict:
    conn = results.get("connection") or {}
    wifi = conn.get("wifi") if isinstance(conn.get("wifi"), dict) else {}
    latency = results.get("latency") or {}
    gateway, internet = latency.get("gateway") or {}, latency.get("internet") or {}
    dns = (results.get("dns") or {}).get("current") or {}
    speed = results.get("speed") or {}
    return {
        "type": "run",
        "time": time.time(),
        "ssid": conn.get("label"),
        "band": wifi.get("band"),
        "signal": wifi.get("signal"),
        "link_mbps": wifi.get("rx_mbps") or conn.get("link_mbps"),
        "gateway_ms": gateway.get("avg_ms"),
        "gateway_loss": gateway.get("loss_pct"),
        "internet_ms": internet.get("avg_ms"),
        "jitter_ms": internet.get("jitter_ms"),
        "loss_pct": internet.get("loss_pct"),
        "dns_ms": dns.get("median_ms"),
        "download_mbps": speed.get("download_mbps"),
        "upload_mbps": speed.get("upload_mbps"),
        "bottleneck": analysis.get("bottleneck"),
        "fixes_on": sorted(fixes_on),
    }


class History:
    def __init__(self, store: Store):
        self.store = store

    def _append(self, entry: dict) -> None:
        self.store.update("history", [], lambda items: (items + [entry])[-MAX_ENTRIES:])

    def add_run(self, entry: dict) -> None:
        self._append(entry)

    def add_event(self, text: str, ssid: str | None = None) -> None:
        self._append({"type": "event", "time": time.time(), "text": text, "ssid": ssid})

    def entries(self) -> list[dict]:
        return list(reversed(self.store.get("history", [])))

    def clear(self) -> None:
        self.store.set("history", [])
