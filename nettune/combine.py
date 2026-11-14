"""Combine two internet connections (for example campus Wi-Fi + USB tethering).

Windows only ever uses one default route. This module runs a small proxy on
127.0.0.1 that speaks HTTP and SOCKS5 and sends each *connection* out through
one of the available uplinks, by binding the outgoing socket to that uplink's
address. Many connections at once (browsing, segmented downloads, several apps)
therefore add up to the sum of the links.

What it cannot do: split one TCP connection across two links. A single stream
is as fast as the link it lands on. True per-packet bonding needs a server on
the far side (MPTCP, Speedify and the like).

Balancing: least active connections, so a slow link that holds connections
longer automatically receives fewer new ones. A host keeps the uplink it was
first given for a while ("sticky"), because sites tie logins to an IP address.
"""
from __future__ import annotations

import ctypes
import ipaddress
import re
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit

from .state import Store
from .system import IS_WINDOWS, CommandError, as_list, run_ps_json

DEFAULT_PORT = 8766
STICKY_SECONDS = 600
STICKY_SLACK = 2  # how much busier (weighted open connections) the sticky uplink may be
MIN_WEIGHT = 0.1
CAPACITY_DECAY = 0.995  # per second: an old speed record fades in a few minutes
AVOID_SECONDS = 1800    # how long a host stays off a link that failed it
STALL_BYTES = 256 * 1024  # an upload this far in with no answer counts as "mid-upload"
SEND_TIMEOUT = 60
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{1,251})[a-z0-9]$")
DOWN_AFTER_FAILURES = 4
DOWN_SECONDS = 30
IDLE_SECONDS = 300
PROBE_HOST = "www.google.com"

_PS_UPLINKS = r"""
$routes = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Where-Object { $_.NextHop -ne '0.0.0.0' } | Sort-Object { $_.RouteMetric + $_.InterfaceMetric }
@($routes | ForEach-Object {
    $a = Get-NetAdapter -InterfaceIndex $_.ifIndex -ErrorAction SilentlyContinue
    $ip = Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($a -and $a.Status -eq 'Up' -and $ip) {
        [pscustomobject]@{
            ifIndex     = [int]$_.ifIndex
            name        = [string]$a.Name
            description = [string]$a.InterfaceDescription
            ip          = [string]$ip.IPAddress
            gateway     = [string]$_.NextHop
            isWifi      = [bool]($a.PhysicalMediaType -match '802\.11')
        }
    }
}) | ConvertTo-Json -Depth 3
"""


class CombineError(RuntimeError):
    pass


class Uplink:
    def __init__(self, if_index: int, name: str, ip: str, description: str = "", gateway: str = "", is_wifi: bool = False):
        self.if_index = if_index
        self.name = name
        self.ip = ip
        self.description = description
        self.gateway = gateway
        self.is_wifi = is_wifi
        self.online = None       # result of the last reachability probe
        self.latency_ms = None
        self.active = 0          # connections open right now
        self.connections = 0     # connections ever assigned
        self.bytes_down = 0
        self.bytes_up = 0
        self.failures = 0        # consecutive connect failures
        self.capacity = 0.0      # fastest rate seen recently, bytes/s (0 = not measured yet)
        self._window_bytes = 0
        self._window_start = 0.0
        self.down_until = 0.0

    def usable(self, now: float) -> bool:
        return now >= self.down_until

    def describe(self) -> dict:
        return {
            "if_index": self.if_index, "name": self.name, "description": self.description,
            "ip": self.ip, "gateway": self.gateway, "is_wifi": self.is_wifi,
            "online": self.online, "latency_ms": self.latency_ms,
            "active": self.active, "connections": self.connections,
            "bytes_down": self.bytes_down, "bytes_up": self.bytes_up,
            "capacity_mbps": round(self.capacity * 8 / 1e6, 2),
            "down": time.time() < self.down_until,
        }


def discover() -> list[Uplink]:
    """Every adapter that is up and has its own route to the internet."""
    try:
        rows = as_list(run_ps_json(_PS_UPLINKS))
    except CommandError as exc:
        raise CombineError(str(exc)) from exc
    uplinks, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("ifIndex") in seen:
            continue
        seen.add(row["ifIndex"])
        uplinks.append(Uplink(row["ifIndex"], row.get("name") or "?", row.get("ip") or "",
                              row.get("description") or "", row.get("gateway") or "", bool(row.get("isWifi"))))
    return uplinks


def probe(uplink: Uplink, timeout: float = 4.0) -> None:
    """Can this uplink reach the internet by itself?"""
    try:
        address = socket.getaddrinfo(PROBE_HOST, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        start = time.perf_counter()
        with socket.create_connection(address, timeout=timeout, source_address=(uplink.ip, 0)):
            uplink.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        uplink.online = True
    except OSError:
        uplink.online, uplink.latency_ms = False, None


def probe_all(uplinks: list[Uplink]) -> None:
    threads = [threading.Thread(target=probe, args=(uplink,)) for uplink in uplinks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class Balancer:
    """Chooses an uplink per connection. Pure logic, no sockets - unit-testable."""

    def __init__(self, uplinks: list[Uplink]):
        self.uplinks = uplinks
        self.lock = threading.Lock()
        self._sticky: dict[str, tuple[Uplink, float]] = {}
        self.rules: dict[str, str] = {}                 # domain -> uplink name ("always use this link")
        self._avoid: dict[tuple[str, str], float] = {}  # (host, uplink name) -> avoid until
        self.hosts: dict[str, dict] = {}                # per-host traffic, for the dashboard

    def rule_for(self, host: str) -> str | None:
        """The link a host is pinned to: the longest matching domain wins."""
        best = None
        for domain, name in self.rules.items():
            if (host == domain or host.endswith("." + domain)) and (best is None or len(domain) > len(best[0])):
                best = (domain, name)
        return best[1] if best else None

    def _ranked(self, host: str, now: float, tried=()) -> list[Uplink]:
        """Uplinks to try for ``host``, best first. Caller holds the lock."""
        candidates = [u for u in self.uplinks if u not in tried]
        usable = [u for u in candidates if u.usable(now)] or candidates
        # Weight each link by the fastest rate it has shown, so a link that is
        # five times faster receives about five times the connections. Until a
        # link has carried traffic all weights are equal. A slow link never
        # drops below MIN_WEIGHT, so it keeps contributing.
        top = max((u.capacity for u in usable), default=0.0)
        weight = {u: (max(u.capacity / top, MIN_WEIGHT) if top > 0 and u.capacity > 0 else 1.0) for u in usable}
        score = {u: (u.active + 1) / weight[u] for u in usable}
        ranked = sorted(usable, key=lambda u: (score[u], u.connections))
        sticky = self._sticky.get(host)
        # Keep a host on its uplink for sequential use (logins are tied to an
        # IP), but let parallel connections to one host spill over to the
        # other link - that is what makes a multi-part download add up.
        if (sticky and now - sticky[1] < STICKY_SECONDS and sticky[0] in usable
                and score[sticky[0]] - score[ranked[0]] < STICKY_SLACK):
            ranked.remove(sticky[0])
            ranked.insert(0, sticky[0])
        # A host that recently failed on a link (blocked site, upload cut off
        # by a firewall) is tried on the other link first.
        avoided = [u for u in ranked if self._avoid.get((host, u.name), 0) > now]
        if avoided and len(avoided) < len(ranked):
            ranked = [u for u in ranked if u not in avoided] + avoided
        # A site the user pinned to a link always tries that link first.
        pinned = self.rule_for(host)
        for uplink in ranked:
            if uplink.name == pinned:
                ranked.remove(uplink)
                ranked.insert(0, uplink)
                break
        return ranked

    def order(self, host: str, now: float | None = None) -> list[Uplink]:
        with self.lock:
            return self._ranked(host, time.time() if now is None else now)

    def reserve(self, host: str, tried=()) -> Uplink | None:
        """Pick the best uplink and count the connection against it at once.

        Choosing and counting must be one atomic step: connections that start
        in the same instant would otherwise all see "zero open" and pile onto
        the same link.
        """
        with self.lock:
            ranked = self._ranked(host, time.time(), tried)
            if not ranked:
                return None
            ranked[0].active += 1
            return ranked[0]

    def opened(self, uplink: Uplink, host: str) -> None:
        """The reserved connection succeeded."""
        with self.lock:
            uplink.connections += 1
            uplink.failures = 0
            if len(self._sticky) > 5000:
                self._sticky.clear()
            self._sticky[host] = (uplink, time.time())

    def failed(self, uplink: Uplink, host: str | None = None) -> None:
        """The reserved connection could not be opened."""
        with self.lock:
            uplink.active = max(0, uplink.active - 1)
            if host:
                self._avoid[(host, uplink.name)] = time.time() + AVOID_SECONDS
            uplink.failures += 1
            if uplink.failures >= DOWN_AFTER_FAILURES:
                uplink.down_until = time.time() + DOWN_SECONDS
                uplink.failures = 0

    def closed(self, uplink: Uplink) -> None:
        with self.lock:
            uplink.active = max(0, uplink.active - 1)

    def trouble(self, host: str, uplink: Uplink) -> None:
        """A connection to ``host`` broke mid-upload on this link: steer the retry elsewhere."""
        with self.lock:
            self._avoid[(host, uplink.name)] = time.time() + AVOID_SECONDS
            self._sticky.pop(host, None)
            self._host(host, uplink)["problems"] += 1

    def _host(self, host: str, uplink: Uplink) -> dict:
        entry = self.hosts.get(host)
        if entry is None:
            if len(self.hosts) >= 400:  # forget the hosts not seen for longest
                for old in sorted(self.hosts, key=lambda h: self.hosts[h]["seen"])[:100]:
                    del self.hosts[old]
            entry = self.hosts[host] = {"up": 0, "down": 0, "problems": 0, "link": uplink.name, "seen": 0.0}
        entry["link"], entry["seen"] = uplink.name, time.time()
        return entry

    def hosts_report(self, limit: int = 15) -> list[dict]:
        now = time.time()
        with self.lock:
            names = sorted(self.hosts, key=lambda h: self.hosts[h]["seen"], reverse=True)[:limit]
            rows = [{
                "host": host, **{k: self.hosts[host][k] for k in ("up", "down", "problems", "link")},
                "pinned": self.rule_for(host),
                "avoiding": [u.name for u in self.uplinks if self._avoid.get((host, u.name), 0) > now],
            } for host in names]
            seen = set(names)
            for domain, name in self.rules.items():
                if domain not in seen:
                    rows.append({"host": domain, "up": 0, "down": 0, "problems": 0, "link": None, "pinned": name, "avoiding": []})
            return rows

    def count(self, uplink: Uplink, down: int = 0, up: int = 0, host: str | None = None) -> None:
        with self.lock:
            uplink.bytes_down += down
            uplink.bytes_up += up
            if host:
                entry = self._host(host, uplink)
                entry["down"] += down
                entry["up"] += up
            now = time.time()
            uplink._window_bytes += down + up
            elapsed = now - uplink._window_start
            if elapsed >= 1.0:
                if uplink._window_start and elapsed < 5.0:
                    rate = uplink._window_bytes / elapsed
                    uplink.capacity = max(rate, uplink.capacity * (CAPACITY_DECAY ** elapsed))
                uplink._window_bytes, uplink._window_start = 0, now


# ------------------------------------------------------------------ proxy

def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("client closed the connection")
        data += chunk
    return data


_SOCKS_OK = b"\x05\x00\x00\x01" + bytes(6)
_SOCKS_FAIL = b"\x05\x05\x00\x01" + bytes(6)
_HTTP_FAIL = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _read_socks5(client: socket.socket):
    _, methods = _recv_exact(client, 2)
    _recv_exact(client, methods)
    client.sendall(b"\x05\x00")  # no authentication: the proxy only listens on 127.0.0.1
    _, command, _, kind = _recv_exact(client, 4)
    if kind == 1:
        host = socket.inet_ntoa(_recv_exact(client, 4))
    elif kind == 3:
        host = _recv_exact(client, _recv_exact(client, 1)[0]).decode("ascii", "replace")
    elif kind == 4:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
    else:
        return None
    port = int.from_bytes(_recv_exact(client, 2), "big")
    if command != 1:  # only CONNECT
        client.sendall(b"\x05\x07\x00\x01" + bytes(6))
        return None
    return host, port, _SOCKS_OK, b"", _SOCKS_FAIL


def _read_http(client: socket.socket):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(65536)
        if not chunk or len(data) > 65536:
            return None
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3:
        return None
    method, target, version = parts
    if method.upper() == b"CONNECT":
        host, _, port = target.decode("ascii", "replace").rpartition(":")
        return host.strip("[]"), int(port), b"HTTP/1.1 200 Connection established\r\n\r\n", rest, _HTTP_FAIL
    url = urlsplit(target.decode("latin-1"))
    if url.scheme != "http" or not url.hostname:
        client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        return None
    path = (url.path or "/") + ("?" + url.query if url.query else "")
    skip = (b"proxy-connection:", b"connection:", b"proxy-authorization:")
    headers = [line for line in lines[1:] if not line.lower().startswith(skip)]
    request = b"\r\n".join([method + b" " + path.encode("latin-1") + b" " + version] + headers + [b"Connection: close"])
    return url.hostname, url.port or 80, b"", request + b"\r\n\r\n" + rest, _HTTP_FAIL


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        combiner: Combiner = self.server.combiner  # type: ignore[attr-defined]
        try:
            client.settimeout(20)
            first = client.recv(1, socket.MSG_PEEK)
            if not first:
                return
            parsed = _read_socks5(client) if first == b"\x05" else _read_http(client)
            if not parsed:
                return
            host, port, ok_reply, prefix, fail_reply = parsed
            upstream, uplink = combiner.connect(host, port)
            if upstream is None:
                client.sendall(fail_reply)
                return
            try:
                if ok_reply:
                    client.sendall(ok_reply)
                if prefix:
                    upstream.sendall(prefix)
                    if uplink:
                        combiner.balancer.count(uplink, up=len(prefix), host=host)
                self._relay(client, upstream, uplink, combiner.balancer, host)
            finally:
                upstream.close()
                if uplink:
                    combiner.balancer.closed(uplink)
        except (OSError, ValueError, ConnectionError):
            return

    @staticmethod
    def _relay(client, upstream, uplink, balancer, host) -> None:
        client.settimeout(SEND_TIMEOUT)
        upstream.settimeout(SEND_TIMEOUT)
        unanswered = 0  # bytes sent up since the server last said anything

        def broke_mid_upload() -> None:
            # Resets are normal on idle connections; only one that cuts off a
            # sizeable upload says "this link cannot carry uploads to this host".
            if uplink and unanswered > STALL_BYTES:
                balancer.trouble(host, uplink)

        while True:
            readable, _, _ = select.select([client, upstream], [], [], IDLE_SECONDS)
            if not readable:
                return
            for source in readable:
                if source is client:
                    data = client.recv(65536)
                    if not data:
                        broke_mid_upload()  # the app gave up waiting
                        return
                    try:
                        upstream.sendall(data)
                    except OSError:
                        broke_mid_upload()
                        return
                    unanswered += len(data)
                    if uplink:
                        balancer.count(uplink, up=len(data), host=host)
                else:
                    try:
                        data = upstream.recv(65536)
                    except OSError:
                        broke_mid_upload()
                        return
                    if not data:
                        broke_mid_upload()
                        return
                    unanswered = 0
                    client.sendall(data)
                    if uplink:
                        balancer.count(uplink, down=len(data), host=host)


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 128


# ------------------------------------------------------------------ Windows system proxy

_INET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_PROXY_VALUES = ("ProxyEnable", "ProxyServer", "ProxyOverride")
_BYPASS = "localhost;127.*;<local>"


def _read_system_proxy() -> dict:
    import winreg
    values = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INET_KEY) as key:
        for name in _PROXY_VALUES:
            try:
                values[name] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                values[name] = None
    return values


def _write_system_proxy(values: dict) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INET_KEY, 0, winreg.KEY_SET_VALUE) as key:
        for name in _PROXY_VALUES:
            value = values.get(name)
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            elif name == "ProxyEnable":
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
    # Tell running apps that the proxy settings changed.
    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
    wininet.InternetSetOptionW(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH


# ------------------------------------------------------------------ controller

class Combiner:
    def __init__(self, store: Store):
        self.store = store
        self.balancer = Balancer([])
        self.server: _ProxyServer | None = None
        self.port = 0
        self.system_proxy = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.server is not None

    def connect(self, host: str, port: int):
        """Open a connection to host:port through the best uplink. Returns (socket, uplink)."""
        try:
            address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        except (OSError, IndexError):
            return None, None
        target = ipaddress.ip_address(address[0])
        if target.is_private or target.is_loopback or target.is_link_local:
            # Local destinations (campus sites, the router) only exist on one network.
            try:
                return socket.create_connection(address, timeout=10), None
            except OSError:
                return None, None
        tried: list[Uplink] = []
        while True:
            uplink = self.balancer.reserve(host, tried)
            if uplink is None:
                return None, None
            try:
                sock = socket.create_connection(address, timeout=8, source_address=(uplink.ip, 0))
            except OSError:
                self.balancer.failed(uplink, host)
                tried.append(uplink)
                continue
            self.balancer.opened(uplink, host)
            return sock, uplink

    def start(self, system_proxy: bool = True, minimum: int = 2, port: int = DEFAULT_PORT) -> dict:
        with self._lock:
            if self.running:
                return self.status()
            uplinks = discover()
            probe_all(uplinks)
            online = [u for u in uplinks if u.online]
            if len(online) < minimum:
                raise CombineError(
                    f"Combining needs {minimum} working internet connections, but only {len(online)} "
                    "was found. Plug the phone in by USB and turn on USB tethering, then try again.")
            self.balancer = Balancer(online)
            self.balancer.rules = dict(self.store.get("combine_rules", {}))
            last_error = None
            for candidate in range(port, port + 10):
                try:
                    server = _ProxyServer(("127.0.0.1", candidate), _Handler)
                    break
                except OSError as exc:
                    last_error = exc
            else:
                raise CombineError(f"Could not open a local port for the combiner: {last_error}")
            server.combiner = self  # type: ignore[attr-defined]
            self.server, self.port = server, candidate
            threading.Thread(target=server.serve_forever, daemon=True).start()
            if system_proxy:
                try:
                    self._set_system_proxy()
                except OSError as exc:
                    self._stop_server()
                    raise CombineError(f"Could not set the Windows proxy: {exc}") from exc
        return self.status()

    def _set_system_proxy(self) -> None:
        if not IS_WINDOWS:
            return
        if self.store.get("combine_proxy") is None:
            self.store.set("combine_proxy", _read_system_proxy())
        _write_system_proxy({"ProxyEnable": 1, "ProxyServer": f"127.0.0.1:{self.port}", "ProxyOverride": _BYPASS})
        self.system_proxy = True

    def restore_system_proxy(self) -> bool:
        """Put the Windows proxy settings back. Safe to call at any time."""
        original = self.store.get("combine_proxy")
        if original is None or not IS_WINDOWS:
            return False
        _write_system_proxy(original)
        self.store.set("combine_proxy", None)
        self.system_proxy = False
        return True

    def _stop_server(self) -> None:
        server, self.server = self.server, None
        if server:
            server.shutdown()
            server.server_close()

    def stop(self) -> None:
        with self._lock:
            try:
                self.restore_system_proxy()
            finally:
                self._stop_server()

    def set_rule(self, domain: str, uplink_name: str | None) -> None:
        """Pin a site to one link (or clear the pin with ``None``)."""
        domain = (domain or "").strip().lower().strip(".")
        if not HOST_RE.match(domain):
            raise CombineError("That is not a valid site name.")
        if uplink_name is not None and uplink_name not in [u.name for u in self.balancer.uplinks]:
            raise CombineError("Unknown connection.")

        def update(rules):
            if uplink_name is None:
                rules.pop(domain, None)
            else:
                rules[domain] = uplink_name
            return rules

        rules = self.store.update("combine_rules", {}, update)
        with self.balancer.lock:
            self.balancer.rules = dict(rules)
            self.balancer._sticky.pop(domain, None)

    def status(self) -> dict:
        if self.running:
            uplinks = self.balancer.uplinks
        else:
            uplinks = discover()
            probe_all(uplinks)
        return {
            "running": self.running,
            "port": self.port if self.running else None,
            "system_proxy": self.system_proxy,
            "ready": sum(1 for u in uplinks if u.online) >= 2,
            "uplinks": [u.describe() for u in uplinks],
            "hosts": self.balancer.hosts_report() if self.running else [],
        }
