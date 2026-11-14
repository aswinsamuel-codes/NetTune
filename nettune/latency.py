"""Latency, jitter and packet loss - to the router and to the internet separately.

Comparing the two is how NetTune tells a bad Wi-Fi link from a slow upstream
network: if the router itself answers slowly or drops packets, the problem is
between the laptop and the access point.

Campus firewalls often block pings and even direct connections to public DNS
addresses, so the internet side is measured two ways at once: ICMP pings, and
TCP handshakes to well-known websites. The handshake time is one round trip, so
the two are comparable.
"""
from __future__ import annotations

import re
import socket
import statistics
import threading
import time

from .system import CommandError, run_cmd

ICMP_TARGETS = [("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8")]
TCP_TARGETS = [("Google", "www.google.com"), ("Cloudflare", "www.cloudflare.com"), ("Microsoft", "www.microsoft.com")]
_REPLY_TIME = re.compile(r"[=<]\s*(\d+)\s*ms", re.I)


def summarise(samples: list[float], sent: int) -> dict:
    received = len(samples)
    stats = {
        "sent": sent,
        "received": received,
        "loss_pct": round(100 * (sent - received) / sent, 1) if sent else None,
        "avg_ms": None,
        "min_ms": None,
        "max_ms": None,
        "jitter_ms": None,
    }
    if samples:
        stats["avg_ms"] = round(statistics.fmean(samples), 1)
        stats["min_ms"] = round(min(samples), 1)
        stats["max_ms"] = round(max(samples), 1)
        diffs = [abs(b - a) for a, b in zip(samples, samples[1:])]
        stats["jitter_ms"] = round(statistics.fmean(diffs), 1) if diffs else 0.0
    return stats


def parse_ping(text: str) -> list[float]:
    """Reply times from ping.exe output. Only reply lines carry a TTL."""
    times = []
    for line in text.splitlines():
        if "TTL=" not in line.upper():
            continue
        match = _REPLY_TIME.search(line)
        if match:
            times.append(float(match.group(1)))
    return times


def ping(host: str, count: int = 10, timeout_ms: int = 1000) -> dict:
    try:
        _, text = run_cmd(
            ["ping", "-n", str(count), "-w", str(timeout_ms), host],
            timeout=count * (timeout_ms / 1000 + 1.5) + 5,
        )
    except CommandError as exc:
        stats = summarise([], count)
        stats.update(method="icmp", error=str(exc))
        return stats
    stats = summarise(parse_ping(text), count)
    stats["method"] = "icmp"
    return stats


def tcp_ping(host: str, port: int = 443, count: int = 8, timeout: float = 2.0) -> dict:
    """Time TCP handshakes. Gives up early when the host is plainly unreachable."""
    samples: list[float] = []
    sent = 0
    try:
        address = socket.gethostbyname(host)
    except OSError:
        address = None
    while address and sent < count:
        sent += 1
        start = time.perf_counter()
        try:
            with socket.create_connection((address, port), timeout=timeout):
                samples.append((time.perf_counter() - start) * 1000)
        except OSError:
            if sent == 3 and not samples:
                break
        time.sleep(0.2)
    stats = summarise(samples, count if samples else max(sent, 1))
    stats["method"] = "tcp"
    return stats


def _score(stats: dict):
    return (stats.get("loss_pct") if stats.get("loss_pct") is not None else 100,
            stats.get("avg_ms") if stats.get("avg_ms") is not None else 1e9)


def run(gateway: str | None, count: int = 10) -> dict:
    results: dict = {}

    def measure(key, fn, *args):
        results[key] = fn(*args)

    threads = []
    if gateway:
        threads.append(threading.Thread(target=measure, args=("gateway", ping, gateway, count)))
    for _, host in ICMP_TARGETS:
        threads.append(threading.Thread(target=measure, args=(host, ping, host, count)))
    for _, host in TCP_TARGETS:
        threads.append(threading.Thread(target=measure, args=(host, tcp_ping, host)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    targets = [{"name": name, "host": host, **results[host]} for name, host in ICMP_TARGETS + TCP_TARGETS]
    answered = [t for t in targets if t["received"]]
    icmp = [t for t in answered if t["method"] == "icmp"]
    # Prefer ICMP (more samples, no handshake overhead); fall back to TCP.
    pool = icmp or answered
    internet = min(pool, key=_score) if pool else {**targets[0]}
    return {
        "gateway": {"host": gateway, **results["gateway"]} if gateway else None,
        "internet": internet,
        "targets": targets,
        "icmp_blocked": not icmp,
    }
