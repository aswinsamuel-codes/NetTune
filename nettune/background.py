"""What is using the connection right now?

Windows does not expose per-process byte counts without a kernel trace, so this
reports what can be measured honestly: total adapter throughput over a short
window, plus the apps that hold open internet connections.
"""
from __future__ import annotations

from .system import CommandError, as_list, run_ps_json

_PS_BACKGROUND = r"""
$a = Get-NetAdapter -InterfaceIndex ([int]$env:NT_IFINDEX)
$s1 = Get-NetAdapterStatistics -Name $a.Name
$t1 = Get-Date
Start-Sleep -Milliseconds 2000
$s2 = Get-NetAdapterStatistics -Name $a.Name
$secs = ((Get-Date) - $t1).TotalSeconds
$conns = Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
    Where-Object { $_.RemoteAddress -notmatch '^(127\.|::1$|0\.0\.0\.0$|::$)' }
$apps = @($conns | Group-Object OwningProcess | ForEach-Object {
    $p = Get-Process -Id ([int]$_.Name) -ErrorAction SilentlyContinue
    [pscustomobject]@{
        pid         = [int]$_.Name
        name        = $(if ($p) { $p.ProcessName } else { 'unknown' })
        connections = [int]$_.Count
    }
} | Sort-Object connections -Descending | Select-Object -First 12)
$bits = 0
try { $bits = @(Get-BitsTransfer -ErrorAction Stop | Where-Object { $_.JobState -eq 'Transferring' }).Count } catch {}
[pscustomobject]@{
    downBps = [math]::Round(($s2.ReceivedBytes - $s1.ReceivedBytes) * 8 / $secs)
    upBps   = [math]::Round(($s2.SentBytes - $s1.SentBytes) * 8 / $secs)
    apps    = $apps
    bits    = [int]$bits
} | ConvertTo-Json -Depth 4
"""

# Process name (lower case) -> what it tends to do with your bandwidth.
KNOWN_HEAVY = {
    "onedrive": "OneDrive file sync",
    "googledrivefs": "Google Drive file sync",
    "dropbox": "Dropbox file sync",
    "steam": "Steam downloads/updates",
    "steamwebhelper": "Steam downloads/updates",
    "epicgameslauncher": "Epic Games downloads/updates",
    "battle.net": "Battle.net downloads/updates",
    "riotclientservices": "Riot client updates",
    "qbittorrent": "Torrent client",
    "utorrent": "Torrent client",
    "bittorrent": "Torrent client",
    "mousocoreworker": "Windows Update",
    "wuauclt": "Windows Update",
    "tiworker": "Windows Update installer",
    "ms-teams": "Microsoft Teams",
    "teams": "Microsoft Teams",
    "zoom": "Zoom call",
    "discord": "Discord",
    "spotify": "Spotify streaming",
}

BUSY_BPS = 1_000_000  # 1 Mbps of traffic while the user is "idle" is worth flagging


def run(if_index: int) -> dict:
    try:
        data = run_ps_json(_PS_BACKGROUND, {"ifindex": if_index}, timeout=45)
    except CommandError as exc:
        return {"error": str(exc), "apps": []}
    apps = []
    for app in as_list((data or {}).get("apps")):
        name = str(app.get("name") or "unknown")
        apps.append({
            "pid": app.get("pid"),
            "name": name,
            "connections": app.get("connections") or 0,
            "note": KNOWN_HEAVY.get(name.lower()),
        })
    down, up = (data or {}).get("downBps") or 0, (data or {}).get("upBps") or 0
    return {
        "down_bps": down,
        "up_bps": up,
        "busy": down >= BUSY_BPS or up >= BUSY_BPS,
        "bits_transfers": (data or {}).get("bits") or 0,
        "apps": apps,
        "heavy": [a for a in apps if a["note"]],
    }
