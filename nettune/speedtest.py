"""Download/upload throughput test (multi-stream, time-bounded, data-capped).

``lite`` mode is meant for metered connections such as a phone hotspot.
"""
from __future__ import annotations

import http.client
import os
import ssl
import threading
import time
import urllib.error
import urllib.request

USER_AGENT = "NetTune-speedtest/0.1"

MODES = {
    "full": {"down_s": 8, "down_cap": 250_000_000, "down_streams": 4,
             "up_s": 6, "up_cap": 60_000_000, "up_streams": 2},
    "lite": {"down_s": 5, "down_cap": 10_000_000, "down_streams": 2,
             "up_s": 4, "up_cap": 3_000_000, "up_streams": 1},
}

# (name, url template, probe url). {n} is the number of bytes requested.
DOWNLOAD_SOURCES = [
    ("Cloudflare", "https://speed.cloudflare.com/__down?bytes={n}", "https://speed.cloudflare.com/__down?bytes=20000"),
    ("OVH", "https://proof.ovh.net/files/100Mb.dat", None),
    ("Tele2", "http://speedtest.tele2.net/100MB.zip", None),
]
UPLOAD_HOST = "speed.cloudflare.com"
UPLOAD_PATH = "/__up"

PROGRESS = {"phase": "idle", "mbps": 0.0, "bytes": 0}
_progress_lock = threading.Lock()


def progress() -> dict:
    with _progress_lock:
        return dict(PROGRESS)


def _set_progress(**values) -> None:
    with _progress_lock:
        PROGRESS.update(values)


class _Meter:
    def __init__(self, cap: int):
        self.cap = cap
        self.total = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def add(self, count: int) -> None:
        with self.lock:
            self.total += count
            if self.total >= self.cap:
                self.stop.set()


def _open(url: str, timeout: float = 10):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    return urllib.request.urlopen(request, timeout=timeout)


def _pick_source():
    """First source that answers, with its time-to-first-byte in ms."""
    errors = []
    for name, template, probe in DOWNLOAD_SOURCES:
        url = probe or template
        try:
            start = time.perf_counter()
            with _open(url, timeout=6) as response:
                response.read(1024)
            return name, template, round((time.perf_counter() - start) * 1000, 1), errors
        except (OSError, urllib.error.URLError, ssl.SSLError) as exc:
            errors.append(f"{name}: {exc}")
    return None, None, None, errors


def _measure(meter: _Meter, duration: float, phase: str) -> float:
    """Sample the meter until time or data budget runs out; return Mbps."""
    start = time.perf_counter()
    samples = [(0.0, 0)]
    while not meter.stop.is_set():
        time.sleep(0.2)
        elapsed = time.perf_counter() - start
        samples.append((elapsed, meter.total))
        recent = [s for s in samples if s[0] >= elapsed - 1.0]
        span = recent[-1][0] - recent[0][0]
        live = (recent[-1][1] - recent[0][1]) * 8 / span / 1e6 if span > 0 else 0.0
        _set_progress(phase=phase, mbps=round(live, 2), bytes=meter.total)
        if elapsed >= duration:
            meter.stop.set()
    end_t, end_b = samples[-1]
    # Skip the TCP ramp-up second when the test ran long enough to allow it.
    ramp = next((s for s in samples if s[0] >= 1.0), None) if end_t >= 2.5 else None
    start_t, start_b = ramp if ramp else (0.0, 0)
    span = end_t - start_t
    return round((end_b - start_b) * 8 / span / 1e6, 2) if span > 0 else 0.0


def _download_worker(meter: _Meter, template: str) -> None:
    while not meter.stop.is_set():
        try:
            with _open(template.format(n=25_000_000)) as response:
                while not meter.stop.is_set():
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    meter.add(len(chunk))
        except (OSError, urllib.error.URLError, ssl.SSLError, http.client.HTTPException):
            if meter.stop.wait(0.3):
                return


def _upload_worker(meter: _Meter, body_size: int, errors: list) -> None:
    block = os.urandom(32768)
    while not meter.stop.is_set():
        connection = http.client.HTTPSConnection(UPLOAD_HOST, timeout=10)
        try:
            connection.putrequest("POST", UPLOAD_PATH)
            connection.putheader("User-Agent", USER_AGENT)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(body_size))
            connection.endheaders()
            sent = 0
            while sent < body_size and not meter.stop.is_set():
                piece = block[: min(len(block), body_size - sent)]
                connection.send(piece)
                sent += len(piece)
                meter.add(len(piece))
            if sent == body_size:
                connection.getresponse().read()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            errors.append(str(exc))
            if meter.stop.wait(0.3):
                return
        finally:
            connection.close()


def _run_phase(meter, duration, phase, target, args, streams):
    threads = [threading.Thread(target=target, args=args, daemon=True) for _ in range(streams)]
    for thread in threads:
        thread.start()
    mbps = _measure(meter, duration, phase)
    for thread in threads:
        thread.join(timeout=3)
    return mbps


def run(mode: str = "full") -> dict:
    config = MODES.get(mode, MODES["full"])
    result = {"mode": mode if mode in MODES else "full", "download_mbps": None, "upload_mbps": None,
              "bytes_down": 0, "bytes_up": 0, "source": None, "ttfb_ms": None, "errors": []}
    try:
        _set_progress(phase="connecting", mbps=0.0, bytes=0)
        name, template, ttfb, errors = _pick_source()
        result["errors"].extend(errors)
        if not template:
            result["errors"].append("No speed test server could be reached.")
            return result
        result["source"], result["ttfb_ms"] = name, ttfb

        meter = _Meter(config["down_cap"])
        result["download_mbps"] = _run_phase(
            meter, config["down_s"], "download", _download_worker, (meter, template), config["down_streams"])
        result["bytes_down"] = meter.total

        upload_errors: list = []
        meter = _Meter(config["up_cap"])
        body = 4_000_000 if result["mode"] == "full" else 1_000_000
        upload = _run_phase(
            meter, config["up_s"], "upload", _upload_worker, (meter, body, upload_errors), config["up_streams"])
        result["bytes_up"] = meter.total
        if meter.total > 0:
            result["upload_mbps"] = upload
        elif upload_errors:
            result["errors"].append("Upload test failed: " + upload_errors[0])
        return result
    finally:
        _set_progress(phase="idle", mbps=0.0, bytes=0)
