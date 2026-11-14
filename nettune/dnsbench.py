"""DNS benchmark: how fast does each resolver answer from *this* network?

Uses a hand-built UDP query so there are no dependencies. Fixes may only switch
to a resolver from ``PROVIDERS`` - the API never accepts arbitrary server IPs.
"""
from __future__ import annotations

import random
import socket
import statistics
import struct
import threading
import time

PROVIDERS = {
    "cloudflare": {"name": "Cloudflare", "servers": ["1.1.1.1", "1.0.0.1"]},
    "google": {"name": "Google", "servers": ["8.8.8.8", "8.8.4.4"]},
    "quad9": {"name": "Quad9", "servers": ["9.9.9.9", "149.112.112.112"]},
    "opendns": {"name": "OpenDNS", "servers": ["208.67.222.222", "208.67.220.220"]},
    "adguard": {"name": "AdGuard", "servers": ["94.140.14.14", "94.140.15.15"]},
}

DOMAINS = [
    "google.com", "youtube.com", "github.com", "wikipedia.org",
    "microsoft.com", "amazon.com", "whatsapp.com", "instagram.com",
]


def provider_for(ip: str | None):
    for pid, provider in PROVIDERS.items():
        if ip in provider["servers"]:
            return pid
    return None


def build_query(name: str, query_id: int) -> bytes:
    header = struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split("."))
    return header + labels + b"\x00" + struct.pack(">HH", 1, 1)


def query(server: str, name: str, timeout: float = 1.5):
    """Round-trip time in ms for one A query, or None on timeout/error."""
    query_id = random.randrange(65536)
    packet = build_query(name, query_id)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            start = time.perf_counter()
            sock.sendto(packet, (server, 53))
            while True:
                data, _ = sock.recvfrom(1024)
                if len(data) >= 4 and data[:2] == packet[:2]:
                    break
            elapsed = (time.perf_counter() - start) * 1000
            rcode = data[3] & 0x0F
            return elapsed if rcode in (0, 3) else None
    except OSError:
        return None


def measure(server: str, domains=DOMAINS) -> dict:
    samples, failures = [], 0
    for index, domain in enumerate(domains):
        result = query(server, domain)
        if result is None:
            failures += 1
            if index == 2 and failures == 3:
                # Three straight timeouts: the resolver is blocked or unreachable.
                failures = len(domains)
                break
        else:
            samples.append(result)
    return {
        "ip": server,
        "queries": len(domains),
        "failures": failures,
        "reachable": bool(samples),
        "median_ms": round(statistics.median(samples), 1) if samples else None,
        "avg_ms": round(statistics.fmean(samples), 1) if samples else None,
    }


def reachable(server: str) -> bool:
    return any(query(server, domain, timeout=1.2) is not None for domain in DOMAINS[:3])


def run(current_servers: list[str]) -> dict:
    """Benchmark the system resolver against the public providers."""
    candidates = []
    current = current_servers[0] if current_servers else None
    current_pid = provider_for(current)
    if current and not current_pid:
        candidates.append({"id": "current", "name": "Current (network default)", "ip": current})
    for pid, provider in PROVIDERS.items():
        candidates.append({"id": pid, "name": provider["name"], "ip": provider["servers"][0]})

    results = [None] * len(candidates)

    def worker(index, candidate):
        results[index] = {**candidate, **measure(candidate["ip"])}

    threads = [threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(candidates)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for entry in results:
        entry["is_current"] = entry["ip"] == current or (current_pid is not None and entry["id"] == current_pid)

    usable = [r for r in results if r["reachable"] and r["failures"] <= 1]
    fastest = min(usable, key=lambda r: r["median_ms"]) if usable else None
    public = [r for r in usable if r["id"] != "current"]
    fastest_public = min(public, key=lambda r: r["median_ms"]) if public else None
    current_entry = next((r for r in results if r["is_current"]), None)
    return {
        "servers": sorted(results, key=lambda r: (r["median_ms"] is None, r["median_ms"] or 0)),
        "current": current_entry,
        "fastest": fastest,
        "fastest_public": fastest_public,
        "public_blocked": not public,
    }
