"""Turns raw test results into plain-language findings.

Each rule is a small, independent check. A finding may point at a fix or action
that the dashboard offers as a one-click button; it is only offered when that
fix is currently applicable (state ``off``).
"""
from __future__ import annotations

_ORDER = {"bad": 0, "warn": 1, "info": 2, "good": 3}


def _finding(severity, title, detail, fix=None, params=None, action=None):
    return {"severity": severity, "title": title, "detail": detail,
            "fix": fix, "fix_params": params, "action": action}


def link_is_bad(gateway: dict | None) -> bool:
    if not gateway or not gateway.get("received"):
        return False
    return (gateway.get("loss_pct") or 0) >= 3 or (gateway.get("jitter_ms") or 0) >= 20 or (gateway.get("avg_ms") or 0) >= 30


def upstream_is_bad(gateway: dict | None, internet: dict | None) -> bool:
    if not internet:
        return False
    if not internet.get("received"):
        return True
    extra = (internet.get("avg_ms") or 0) - ((gateway or {}).get("avg_ms") or 0)
    return (internet.get("loss_pct") or 0) >= 3 or (internet.get("avg_ms") or 0) >= 150 or extra >= 120


def analyze(results: dict, fix_states: dict | None = None) -> dict:
    fix_states = fix_states or {}

    def offer(fix_id):
        return fix_id if fix_states.get(fix_id) == "off" else None

    conn = results.get("connection") or {}
    if not conn.get("connected"):
        return {
            "headline": "No active internet connection was found.",
            "bottleneck": "offline",
            "findings": [_finding("bad", "Not connected", "Connect to Wi-Fi or a hotspot, then run the diagnosis again.")],
        }

    findings = []
    wifi = conn.get("wifi") if isinstance(conn.get("wifi"), dict) else None
    hotspot = bool(conn.get("hotspot_likely"))
    if wifi and wifi.get("error"):
        findings.append(_finding("info", "Wi-Fi details are hidden by Windows", wifi.get("message") or ""))
        wifi = None
    nearby = (results.get("nearby") or {}).get("summary") or {}
    latency = results.get("latency") or {}
    gateway, internet = latency.get("gateway"), latency.get("internet")
    link_bad = link_is_bad(gateway)
    upstream_bad = not link_bad and upstream_is_bad(gateway, internet)

    # --- radio link ---------------------------------------------------------
    if wifi:
        signal = wifi.get("signal")
        if signal is not None and signal < 40:
            where = "Keep the phone next to the laptop, in line of sight." if hotspot else \
                "Move closer to an access point or away from walls and metal."
            findings.append(_finding("bad", f"Weak Wi-Fi signal ({signal}%)", where + " No software setting can replace signal strength."))
        elif signal is not None and signal < 60:
            findings.append(_finding("warn", f"Fair Wi-Fi signal ({signal}%)", "Speed drops quickly below about 60%. A spot closer to the access point will help."))

        if wifi.get("band") == "2.4":
            if nearby.get("has_5ghz_same_ssid") or nearby.get("has_6ghz_same_ssid"):
                findings.append(_finding(
                    "warn", "Connected on 2.4 GHz although 5 GHz is available",
                    "This network also broadcasts on a faster band within range. Prefer 5 GHz, then reconnect.",
                    fix=offer("prefer_5ghz"), action="reconnect_wifi"))
            elif hotspot:
                findings.append(_finding(
                    "warn", "Your hotspot is broadcasting on 2.4 GHz",
                    "In the phone's hotspot settings, switch the band to 5 GHz. USB tethering is faster and steadier still."))
            else:
                findings.append(_finding("info", "This network only offers 2.4 GHz here", "2.4 GHz is slower and more crowded; no 5 GHz signal for this network was seen in range."))

        better = nearby.get("better_ap")
        if better:
            findings.append(_finding(
                "warn", "A stronger access point is in range",
                f"Another access point of this network has {better['signal']}% signal versus your {wifi.get('signal')}%. "
                "Reconnect to jump to it now; raise roaming aggressiveness to make the laptop switch on its own.",
                fix=offer("roaming"), action="reconnect_wifi"))

        crowded = nearby.get("same_channel")
        limit = 8 if wifi.get("band") == "2.4" else 4
        if crowded is not None and crowded >= limit:
            findings.append(_finding(
                "info", f"Crowded channel ({crowded} other access points overlap channel {wifi.get('channel')})",
                "Access points sharing a channel take turns to transmit. On your own hotspot or router, pick another channel or band."))

    rate = (wifi or {}).get("rx_mbps") or (conn.get("link_mbps") if conn.get("is_wifi") else None)
    if rate and rate < 60:
        findings.append(_finding(
            "warn", f"Low Wi-Fi link rate ({rate:.0f} Mbps)",
            f"The laptop and the access point have negotiated only {rate:.0f} Mbps, and real speed is usually about half "
            "of that figure. This happens with a weak signal, a 2.4 GHz connection or heavy interference. Moving closer "
            "or getting onto 5 GHz raises it.",
            fix=offer("prefer_5ghz"), action="reconnect_wifi" if wifi else None))

    # --- latency ------------------------------------------------------------
    if gateway and not gateway.get("received") and internet and internet.get("received"):
        findings.append(_finding("info", "The router does not answer pings", "Common on campus networks. It only means the Wi-Fi hop cannot be measured on its own."))
    if link_bad:
        findings.append(_finding(
            "bad", "The Wi-Fi link to the router is unstable",
            f"Pinging the router itself shows {gateway.get('avg_ms')} ms average, {gateway.get('jitter_ms')} ms jitter and "
            f"{gateway.get('loss_pct')}% loss. That hop should be a few ms with no loss, so the problem is between "
            "this laptop and the access point: signal, interference, band or driver."))
    elif upstream_bad:
        link = f"clean ({gateway.get('avg_ms')} ms, {gateway.get('loss_pct')}% loss)" if gateway and gateway.get("received") else "not the slow part"
        cause = "the phone's mobile signal or carrier" if hotspot else "the campus network or its internet uplink"
        findings.append(_finding(
            "bad", "The slowdown is beyond your laptop",
            f"The hop to the router is {link}, but the internet answers in {internet.get('avg_ms')} ms with "
            f"{internet.get('loss_pct')}% loss. That points at {cause}. Settings on this laptop cannot fix it; "
            "try another location or time of day."))
    elif internet and internet.get("received"):
        findings.append(_finding("good", "Latency is healthy", f"Internet round trip {internet.get('avg_ms')} ms with {internet.get('loss_pct')}% loss."))

    # --- DNS ----------------------------------------------------------------
    dns = results.get("dns") or {}
    current, best = dns.get("current"), dns.get("fastest_public")
    if dns.get("servers"):
        if dns.get("public_blocked"):
            findings.append(_finding("info", "This network blocks public DNS servers", "Only the network's own DNS works here, so it has to stay."))
        elif current and not current.get("reachable") and best:
            findings.append(_finding("bad", "The current DNS server is not answering", f"{best['name']} answers in {best['median_ms']} ms from here.",
                                     fix=offer("dns"), params={"provider": best["id"]}))
        elif current and best and current.get("median_ms") is not None and current["median_ms"] - best["median_ms"] >= 15:
            findings.append(_finding(
                "warn", "DNS lookups are slow",
                f"The current DNS takes {current['median_ms']} ms per lookup; {best['name']} takes {best['median_ms']} ms from this network. "
                "Every new site you open pays that delay.",
                fix=offer("dns"), params={"provider": best["id"]}))
        elif current and current.get("median_ms") is not None:
            findings.append(_finding("good", "DNS is fast enough", f"Lookups take {current['median_ms']} ms."))

    # --- settings -----------------------------------------------------------
    if offer("power_saving"):
        findings.append(_finding("warn", "Wi-Fi power saving is on", "Windows lets the Wi-Fi radio doze to save battery, which costs speed and adds lag.", fix="power_saving"))
    if offer("tcp_autotuning"):
        findings.append(_finding("warn", "TCP auto-tuning is not at its default", "Something changed the receive-window setting; that can cap download speed.", fix="tcp_autotuning"))
    if hotspot and offer("metered"):
        findings.append(_finding("info", "This hotspot is not marked as metered", "Marking it metered stops Windows Update and cloud sync from eating mobile data and speed.", fix="metered"))
    if offer("delivery_optimization"):
        findings.append(_finding("info", "Windows may upload updates to other PCs", "Delivery Optimization can use your upload bandwidth in the background.", fix="delivery_optimization"))

    age = conn.get("driver_age_days")
    if conn.get("is_wifi") and age is not None and age > 730:
        findings.append(_finding(
            "warn", f"The Wi-Fi driver is {age // 365} years old",
            f"Installed driver: {conn.get('driver_provider') or ''} {conn.get('driver_version') or ''} dated {conn.get('driver_date')}. "
            "Old Wi-Fi drivers cause drops and low speeds; install the latest one from your laptop maker's support page."))

    # --- background use -----------------------------------------------------
    background = results.get("background") or {}
    heavy = ", ".join(sorted({a["note"] for a in background.get("heavy") or []}))
    if background.get("busy"):
        mbps = max(background.get("down_bps", 0), background.get("up_bps", 0)) / 1e6
        detail = f"About {mbps:.1f} Mbps was in use while the test was idle."
        if heavy:
            detail += f" Likely sources that are running: {heavy}."
        findings.append(_finding("warn", "Something is using the connection in the background", detail))
    elif heavy:
        findings.append(_finding("info", "Bandwidth-heavy apps are running", f"Not busy right now, but these can start at any time: {heavy}."))

    # --- throughput ---------------------------------------------------------
    speed = results.get("speed") or {}
    down = speed.get("download_mbps")
    if down is not None and rate and not link_bad and down < 0.15 * rate:
        cause = "the mobile data connection" if hotspot else "the network's own speed cap or uplink"
        findings.append(_finding(
            "info", "The Wi-Fi link is far faster than the internet behind it",
            f"Your link to the access point is rated {rate:.0f} Mbps, but {down} Mbps arrived from the internet. "
            f"The ceiling is {cause}, not this laptop."))

    findings.sort(key=lambda item: _ORDER[item["severity"]])
    fixable = sum(1 for item in findings if item["fix"])
    if link_bad:
        headline, bottleneck = "Main bottleneck: the Wi-Fi link between this laptop and the access point.", "wifi-link"
    elif upstream_bad:
        headline, bottleneck = "Main bottleneck: the network beyond your laptop, which local settings cannot change.", "upstream"
    elif fixable:
        plural = "setting" if fixable == 1 else "settings"
        headline, bottleneck = f"{fixable} {plural} on this laptop can be improved.", "settings"
    else:
        headline, bottleneck = "No local bottleneck found - this laptop is getting what the network offers.", "none"
    return {"headline": headline, "bottleneck": bottleneck, "findings": findings}
