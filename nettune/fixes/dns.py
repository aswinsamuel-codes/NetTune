"""Switch the adapter to a faster public DNS resolver."""
from __future__ import annotations

import ipaddress

from .. import dnsbench
from ..system import as_list, run_ps, run_ps_json
from .base import OFF, ON, Context, Fix, FixError, status

_PS_STATUS = r"""
$i = [int]$env:NT_IFINDEX
$a = Get-NetAdapter -InterfaceIndex $i
$servers = (Get-DnsClientServerAddress -InterfaceIndex $i -AddressFamily IPv4).ServerAddresses
$key = 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\' + $a.InterfaceGuid
$manual = (Get-ItemProperty -Path $key -Name NameServer -ErrorAction SilentlyContinue).NameServer
[pscustomobject]@{ servers = @($servers); static = [bool]$manual } | ConvertTo-Json
"""

_PS_SET = r"""
Set-DnsClientServerAddress -InterfaceIndex ([int]$env:NT_IFINDEX) -ServerAddresses ($env:NT_SERVERS -split ',')
Clear-DnsClientCache
"""

_PS_RESET = r"""
Set-DnsClientServerAddress -InterfaceIndex ([int]$env:NT_IFINDEX) -ResetServerAddresses
Clear-DnsClientCache
"""

MIN_GAIN_MS = 10  # "auto" only switches when the gain is worth it


class DnsFix(Fix):
    id = "dns"
    title = "Use a faster DNS resolver"
    summary = "Points this adapter at the fastest public DNS server reachable from your network."
    details = (
        "Every site you open starts with a DNS lookup, and campus or hotspot resolvers are "
        "often slow. This changes the IPv4 DNS servers of the active adapter. Trade-off: "
        "college-only websites or login portals may need the college's own DNS - revert if "
        "one stops opening. The setting follows the adapter, so it applies on every Wi-Fi "
        "network until you revert it."
    )
    category = "dns"

    def _read(self, ctx: Context) -> dict:
        data = run_ps_json(_PS_STATUS, {"ifindex": ctx.if_index}) or {}
        return {"servers": [s for s in as_list(data.get("servers")) if s], "static": bool(data.get("static"))}

    def status(self, ctx: Context) -> dict:
        current = self._read(ctx)
        first = current["servers"][0] if current["servers"] else None
        provider = dnsbench.provider_for(first)
        options = [{"id": pid, "name": p["name"]} for pid, p in dnsbench.PROVIDERS.items()]
        if provider:
            name = dnsbench.PROVIDERS[provider]["name"]
            return status(ON, f"Using {name} ({first}).", provider=provider, options=options)
        label = first or "none"
        return status(OFF, f"Using the network's default DNS ({label}).", provider=None, options=options)

    def _choose(self, ctx: Context, only_if_faster: bool):
        bench = dnsbench.run(ctx.conn.get("dns") or [])
        best = bench["fastest_public"]
        if not best:
            raise FixError("This network blocks public DNS servers, so the default DNS has to stay.")
        current = bench["current"]
        if only_if_faster and current and current["reachable"]:
            gain = current["median_ms"] - best["median_ms"]
            if gain < MIN_GAIN_MS:
                return None, f"Kept the current DNS - it is already within {MIN_GAIN_MS} ms of the fastest option."
        return best["id"], None

    def apply(self, ctx: Context, params: dict) -> str:
        provider = (params or {}).get("provider") or "auto"
        if provider == "auto":
            provider, note = self._choose(ctx, bool((params or {}).get("only_if_faster")))
            if provider is None:
                return note
        if provider not in dnsbench.PROVIDERS:
            raise FixError("Unknown DNS provider.")
        entry = dnsbench.PROVIDERS[provider]
        if not dnsbench.reachable(entry["servers"][0]):
            raise FixError(f"{entry['name']} DNS is not reachable from this network, so nothing was changed.")
        self.remember(ctx, self._read(ctx))
        run_ps(_PS_SET, {"ifindex": ctx.if_index, "servers": ",".join(entry["servers"])})
        return f"DNS set to {entry['name']} ({entry['servers'][0]})."

    def revert(self, ctx: Context) -> str:
        saved = self.saved(ctx)
        original = (saved or {}).get("original") or {}
        servers = []
        for server in original.get("servers") or []:
            try:
                servers.append(str(ipaddress.ip_address(server)))
            except ValueError:
                continue
        if original.get("static") and servers:
            run_ps(_PS_SET, {"ifindex": ctx.if_index, "servers": ",".join(servers)})
            message = "DNS restored to " + ", ".join(servers) + "."
        else:
            run_ps(_PS_RESET, {"ifindex": ctx.if_index})
            message = "DNS restored to the network's automatic setting."
        self.forget(ctx)
        return message
