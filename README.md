# NetTune

**Find out why your Wi-Fi is slow, fix what can be fixed, and prove it with numbers.**

NetTune is an open-source network diagnosis and optimization tool for Windows laptops. A small engine runs on your own computer and serves a dashboard in your browser. It was built for two everyday cases: crowded campus Wi-Fi and phone hotspots.

- No installation, no account, no dependencies beyond Python.
- Everything stays on your machine. The only traffic it creates is the tests themselves.
- Every change it makes is saved first and can be reverted with one click.

## What it can and cannot do

No software can make a network faster than its source. If the campus caps each user, or your phone has weak mobile signal, that ceiling stays.

What NetTune does is find the bottleneck and remove the ones on your side:

| It tells you | How |
|---|---|
| Is it my Wi-Fi link, or the network beyond it? | Measures the router and the internet separately (latency, jitter, loss) |
| Is DNS slowing every page down? | Benchmarks your DNS against public resolvers from your network |
| Am I on the wrong band or access point? | Reads signal, band, channel and nearby access points |
| Is something eating my bandwidth? | Measures idle traffic and lists apps holding connections |
| How fast is it really? | Multi-stream download and upload test, with a light mode for hotspots |

Campus firewalls often block pings and public DNS. NetTune detects that and switches to measuring TCP handshakes to well-known sites instead.

## Quick start

Requirements: Windows 10 or 11, and [Python 3.9+](https://www.python.org/downloads/).

1. Download or clone this repository.
2. Double-click `start.bat` (or run `python -m nettune` in the folder).
3. The dashboard opens at `http://127.0.0.1:8765`. Press **Run diagnosis**.

Some fixes change system settings and need administrator rights. Use **Restart as administrator** in the dashboard when you want those; diagnosis works without it.

> Windows 11 hides Wi-Fi details (signal, band, nearby networks) from all apps until Location services are on: Settings > Privacy & security > Location. NetTune still runs without it, with less detail.

## Fixes

Each fix changes exactly one setting. The original value is saved before the change and restored by **Revert**.

| Fix | What it changes | Admin |
|---|---|---|
| Use a faster DNS resolver | IPv4 DNS servers of the active adapter (Cloudflare, Google, Quad9, OpenDNS or AdGuard) | yes |
| Prefer the 5 GHz band | Wi-Fi driver's preferred-band property | yes |
| Roam to closer access points sooner | Wi-Fi driver's roaming aggressiveness | yes |
| Turn off Wi-Fi power saving | Wireless power mode in the current power plan | no |
| Restore TCP auto-tuning | Receive-window auto-tuning back to the Windows default | yes |
| Stop sharing Windows updates with other PCs | Delivery Optimization peer-to-peer policy | yes |
| Treat this network as metered | Cost setting of the current Wi-Fi profile | no |

One-time actions: flush the DNS cache, and reconnect to the strongest access point.

NetTune deliberately leaves out the registry "TCP tweaks" that many optimizer apps apply. On current Windows they do nothing useful or make things worse.

## Combine two connections

The **Combine** tab uses two internet connections at once, for example campus Wi-Fi plus your phone's mobile data over **USB tethering**. (A Wi-Fi hotspot cannot be used for this: one Wi-Fi card joins one Wi-Fi network at a time.)

NetTune runs a local proxy (HTTP and SOCKS5, on `127.0.0.1` only) and sends each connection out through one of the links, favouring the one with fewer open connections. A host keeps the link it was first given for ten minutes, because sites tie logins to an IP address.

- Browsing, multi-part downloads and several apps at once get the sum of both links.
- One single stream still travels over one link. Splitting one connection across two links needs a server on the far side, which NetTune does not use.
- It can set the Windows proxy to itself so browsers and most apps use it. Your previous proxy setting is restored when you stop, when NetTune exits, or the next time it starts after a crash.
- It uses your mobile data.

## Profiles

A profile is a set of fixes for one kind of network. Two are built in, **College Wi-Fi** and **Phone hotspot**, and you can change which fixes each contains. Tie a profile to a network name and NetTune applies it automatically when you join that network (while the engine is running).

## History

Every diagnosis is saved locally. The History tab compares each run with the previous one on the same network, so you can see whether a change actually helped.

## Security model

The engine can change system settings, so the local API is locked down:

- It listens on `127.0.0.1` only and is never reachable from the network.
- Requests must carry a `Host` header for `127.0.0.1` or `localhost`, which blocks DNS-rebinding attacks from websites.
- API calls must carry a random per-run token in a custom header, which blocks cross-site request forgery.
- The API exposes a fixed list of actions. It never runs caller-supplied commands and never accepts free-form values such as DNS addresses.
- Network names come from the air and are treated as untrusted: they are never interpolated into PowerShell scripts or inserted into the page as HTML.

Other programs running under your own Windows account can reach the local API, as they can reach anything else you can. Found a problem? Please open an issue marked `security`.

## How it is built

```
nettune/
  __main__.py      entry point: starts the server, opens the browser
  server.py        local HTTP server, static files, API security
  engine.py        API routing and orchestration
  system.py        wrappers for PowerShell, netsh, ping, powercfg
  netinfo.py       active adapter, router, DNS, driver
  wifi.py          parsers for netsh wlan output
  latency.py       router vs internet latency, jitter, loss
  dnsbench.py      DNS benchmark (hand-built UDP queries)
  speedtest.py     download/upload throughput
  background.py    idle traffic and apps holding connections
  advisor.py       turns results into plain-language findings
  fixes/           one class per reversible fix
  combine.py       two-connection balancing proxy
  profiles.py      profiles and automatic apply
  history.py       saved runs
  web/             dashboard (plain HTML, CSS and JavaScript)
tests/             unit tests for parsers and rules
```

Python standard library only. No build step for the dashboard.

State is stored in `%LOCALAPPDATA%\NetTune\state.json`.

## Known limits

- Windows only for now.
- `netsh` output is parsed in English; other display languages may show less Wi-Fi detail.
- The DNS fix sets IPv4 servers. On networks that hand out IPv6 DNS, Windows may still prefer those.
- Per-app bandwidth is not shown, because Windows does not expose it without a kernel trace. NetTune shows total idle traffic and which apps hold connections.

## Roadmap

- Linux and macOS backends
- Per-app traffic via ETW
- Driver update check against vendor releases
- Custom profiles and more fixes (contributions welcome)
- Charts of speed and latency over time
- Translations

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). New fixes, parsers for other languages and platform backends are all welcome.

## License

[MIT](LICENSE)
