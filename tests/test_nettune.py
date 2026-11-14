"""Unit tests for the pure parts of NetTune: parsers, statistics and advice rules."""
import os
import tempfile
import unittest

from nettune import advisor, dnsbench, latency, netinfo, wifi
from nettune.combine import Balancer, Uplink
from nettune.fixes.adapter import pick_value
from nettune.fixes.power import parse_power_query
from nettune.fixes.tweaks import parse_profile_cost
from nettune.state import Store

INTERFACES = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : MediaTek Wi-Fi 6E MT7922 (RZ616) 160MHz PCIe Adapter
    GUID                   : 42f7a390-05d6-46bd-b86d-5571ab747f5d
    Physical address       : aa:bb:cc:dd:ee:01
    State                  : connected
    SSID                   : Sam’s iPhone
    AP BSSID               : 12:34:56:78:9A:BC
    Band                   : 2.4 GHz
    Channel                : 6
    Network type           : Infrastructure
    Radio type             : 802.11ax
    Authentication         : WPA2-Personal
    Receive rate (Mbps)    : 144.4
    Transmit rate (Mbps)   : 130
    Signal                 : 72%
    Profile                : Sam’s iPhone
"""

NETWORKS = """
Interface name : Wi-Fi
There are 2 networks currently visible.

SSID 1 : Campus
    Network type            : Infrastructure
    Authentication          : WPA2-Enterprise
    Encryption              : CCMP
    BSSID 1                 : aa:aa:aa:aa:aa:01
         Signal             : 55%
         Radio type         : 802.11n
         Band               : 2.4 GHz
         Channel            : 6
         Bss Load:
             Connected Stations:        14
             Channel Utilization:       120 (47 %)
    BSSID 2                 : aa:aa:aa:aa:aa:02
         Signal             : 80%
         Radio type         : 802.11ac
         Band               : 5 GHz
         Channel            : 36

SSID 2 : Neighbour
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    BSSID 1                 : bb:bb:bb:bb:bb:01
         Signal             : 40%
         Radio type         : 802.11n
         Channel            : 8
"""

PING = """
Pinging 10.3.3.254 with 32 bytes of data:
Reply from 10.3.3.254: bytes=32 time=4ms TTL=64
Reply from 10.3.3.254: bytes=32 time<1ms TTL=64
Request timed out.
Reply from 10.3.3.254: bytes=32 time=10ms TTL=64

Ping statistics for 10.3.3.254:
    Packets: Sent = 4, Received = 3, Lost = 1 (25% loss),
Approximate round trip times in milli-seconds:
    Minimum = 1ms, Maximum = 10ms, Average = 5ms
"""


class WifiParsing(unittest.TestCase):
    def test_interface(self):
        iface = wifi.parse_interfaces(INTERFACES)[0]
        self.assertTrue(iface["connected"])
        self.assertEqual(iface["ssid"], "Sam’s iPhone")
        self.assertEqual(iface["bssid"], "12:34:56:78:9a:bc")
        self.assertEqual((iface["band"], iface["channel"], iface["signal"]), ("2.4", 6, 72))
        self.assertEqual(iface["rx_mbps"], 144.4)
        self.assertEqual(iface["generation"], "Wi-Fi 6")

    def test_networks(self):
        nets = wifi.parse_networks(NETWORKS)
        self.assertEqual(len(nets), 3)
        self.assertEqual(nets[0]["utilization"], 47)
        self.assertEqual(nets[0]["stations"], 14)
        self.assertEqual(nets[1]["band"], "5")
        self.assertEqual(nets[2]["band"], "2.4")  # inferred from the channel

    def test_scan_summary(self):
        nets = wifi.parse_networks(NETWORKS)
        mine = {"ssid": "Campus", "bssid": "aa:aa:aa:aa:aa:01", "channel": 6, "band": "2.4", "signal": 55}
        summary = wifi.summarise_scan(nets, mine)
        self.assertTrue(summary["has_5ghz_same_ssid"])
        self.assertEqual(summary["better_ap"]["bssid"], "aa:aa:aa:aa:aa:02")
        self.assertEqual(summary["same_channel"], 1)  # channel 8 overlaps channel 6


class Latency(unittest.TestCase):
    def test_parse_ping_ignores_summary_lines(self):
        self.assertEqual(latency.parse_ping(PING), [4.0, 1.0, 10.0])

    def test_summary(self):
        stats = latency.summarise([4.0, 1.0, 10.0], 4)
        self.assertEqual(stats["loss_pct"], 25.0)
        self.assertEqual(stats["avg_ms"], 5.0)
        self.assertEqual(stats["jitter_ms"], 6.0)

    def test_no_replies(self):
        stats = latency.summarise([], 4)
        self.assertEqual(stats["loss_pct"], 100.0)
        self.assertIsNone(stats["avg_ms"])


class SmallParsers(unittest.TestCase):
    def test_dns_query_packet(self):
        packet = dnsbench.build_query("example.com", 0x1234)
        self.assertEqual(packet[:2], b"\x12\x34")
        self.assertIn(b"\x07example\x03com\x00", packet)
        self.assertEqual(dnsbench.provider_for("1.0.0.1"), "cloudflare")
        self.assertIsNone(dnsbench.provider_for("192.168.0.1"))

    def test_power_query(self):
        text = "Possible Setting Index: 003\nCurrent AC Power Setting Index: 0x00000000\nCurrent DC Power Setting Index: 0x00000002\n"
        self.assertEqual(parse_power_query(text), (0, 2))
        self.assertIsNone(parse_power_query("nothing here"))

    def test_adapter_value_choice(self):
        bands = ["1. No Preference", "2. Prefer 2.4GHz band", "3. Prefer 5GHz band"]
        self.assertEqual(pick_value(bands, [r"(?<![\d.])5\s*G"]), "3. Prefer 5GHz band")
        roaming = ["1. Lowest", "3. Medium-Low", "5. Medium-High", "7. Highest"]
        prefs = [r"medium[\s-]*high", r"^(?!.*highest).*high", r"highest"]
        self.assertEqual(pick_value(roaming, prefs), "5. Medium-High")
        self.assertEqual(pick_value(["Low", "Middle", "High"], prefs), "High")

    def test_profile_cost(self):
        self.assertEqual(parse_profile_cost("Cost settings\n    Cost                   : Unrestricted\n"), "Unrestricted")

    def test_link_speed(self):
        self.assertEqual(netinfo.parse_link_speed("866.7 Mbps"), 866.7)
        self.assertEqual(netinfo.parse_link_speed("1 Gbps"), 1000.0)
        self.assertIsNone(netinfo.parse_link_speed(""))

    def test_hotspot_guess(self):
        self.assertTrue(netinfo.looks_like_hotspot("172.20.10.1", "anything"))
        self.assertTrue(netinfo.looks_like_hotspot("10.0.0.1", "Sam’s iPhone"))
        self.assertFalse(netinfo.looks_like_hotspot("10.3.3.254", "Campus"))


class Advice(unittest.TestCase):
    def _base(self, **latency_overrides):
        gateway = {"received": 10, "avg_ms": 3.0, "jitter_ms": 1.0, "loss_pct": 0.0}
        internet = {"received": 10, "avg_ms": 30.0, "jitter_ms": 2.0, "loss_pct": 0.0}
        gateway.update(latency_overrides.get("gateway", {}))
        internet.update(latency_overrides.get("internet", {}))
        return {
            "connection": {"connected": True, "is_wifi": True, "wifi": {"ssid": "Campus", "signal": 80, "band": "5", "rx_mbps": 400}},
            "latency": {"gateway": gateway, "internet": internet},
        }

    def test_offline(self):
        self.assertEqual(advisor.analyze({"connection": {"connected": False}})["bottleneck"], "offline")

    def test_bad_wifi_link(self):
        result = advisor.analyze(self._base(gateway={"loss_pct": 8.0}))
        self.assertEqual(result["bottleneck"], "wifi-link")

    def test_upstream(self):
        result = advisor.analyze(self._base(internet={"avg_ms": 400.0}))
        self.assertEqual(result["bottleneck"], "upstream")

    def test_healthy(self):
        self.assertEqual(advisor.analyze(self._base())["bottleneck"], "none")

    def test_fix_only_offered_when_applicable(self):
        results = self._base()
        offered = advisor.analyze(results, {"power_saving": "off"})
        self.assertTrue(any(f["fix"] == "power_saving" for f in offered["findings"]))
        self.assertEqual(offered["bottleneck"], "settings")
        not_offered = advisor.analyze(results, {"power_saving": "on"})
        self.assertFalse(any(f["fix"] == "power_saving" for f in not_offered["findings"]))

    def test_slow_dns_recommends_fastest(self):
        results = self._base()
        results["dns"] = {
            "servers": [{}],
            "current": {"reachable": True, "median_ms": 80.0},
            "fastest_public": {"id": "cloudflare", "name": "Cloudflare", "median_ms": 12.0},
            "public_blocked": False,
        }
        finding = next(f for f in advisor.analyze(results, {"dns": "off"})["findings"] if f["fix"] == "dns")
        self.assertEqual(finding["fix_params"], {"provider": "cloudflare"})


class Balancing(unittest.TestCase):
    def setUp(self):
        self.wifi = Uplink(1, "Wi-Fi", "10.0.0.2")
        self.phone = Uplink(2, "Phone", "192.168.42.5")
        self.balancer = Balancer([self.wifi, self.phone])

    def _open(self, host):
        uplink = self.balancer.reserve(host)
        self.balancer.opened(uplink, host)
        return uplink

    def test_simultaneous_connections_alternate(self):
        # reserve() counts at once, so six connections starting together split 3/3.
        picks = [self.balancer.reserve(f"host{i}.example") for i in range(6)]
        self.assertEqual(picks.count(self.wifi), 3)
        self.assertEqual(picks.count(self.phone), 3)

    def test_host_sticks_to_its_uplink(self):
        first = self._open("bank.example")
        self.balancer.closed(first)
        other = self._open("other.example")  # same link again: fewest total connections is the other one
        self.assertIsNot(other, first)
        self.assertIs(self.balancer.order("bank.example")[0], first)

    def test_parallel_connections_to_one_host_spill_over(self):
        picks = [self.balancer.reserve("cdn.example") for _ in range(6)]
        self.assertGreaterEqual(picks.count(self.wifi), 2)
        self.assertGreaterEqual(picks.count(self.phone), 2)

    def test_failing_uplink_is_rested_but_never_all(self):
        for _ in range(4):
            self.balancer.failed(self.phone)
        self.assertEqual(self.balancer.order("x.example"), [self.wifi])
        for _ in range(4):
            self.balancer.failed(self.wifi)
        self.assertEqual(len(self.balancer.order("x.example")), 2)  # all down: still try

    def test_failed_connect_moves_to_the_other_uplink(self):
        first = self.balancer.reserve("a.example")
        self.balancer.failed(first)
        second = self.balancer.reserve("a.example", [first])
        self.assertIsNot(first, second)
        self.assertEqual(first.active, 0)
        self.assertIsNone(self.balancer.reserve("a.example", [first, second]))

    def test_faster_link_gets_more_connections(self):
        self.wifi.capacity, self.phone.capacity = 1_000_000, 5_000_000
        picks = [self.balancer.reserve(f"host{i}.example") for i in range(6)]
        self.assertEqual(picks.count(self.phone), 5)
        self.assertEqual(picks.count(self.wifi), 1)

    def test_counters(self):
        uplink = self._open("a.example")
        self.balancer.count(uplink, down=100, up=7)
        self.balancer.closed(uplink)
        self.assertEqual((uplink.active, uplink.connections, uplink.bytes_down, uplink.bytes_up), (0, 1, 100, 7))


class StoreTests(unittest.TestCase):
    def test_roundtrip_and_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "state.json")
            store = Store(path)
            store.set("fixes", {"a": 1})
            value = store.get("fixes")
            value["a"] = 2  # must not leak back into the store
            self.assertEqual(Store(path).get("fixes"), {"a": 1})
            store.update("history", [], lambda items: items + ["x"])
            self.assertEqual(Store(path).get("history"), ["x"])


if __name__ == "__main__":
    unittest.main()
