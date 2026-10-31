"""Run: python3 -I -m unittest discover -s tools/tests -t tools (from the repo root). No root needed."""
import ipaddress
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "ansible", "roles", "netgroups", "files"))
import netgroups_core as core  # noqa: E402

GOLDEN = os.path.join(HERE, "golden")
RID = "0123456789abcdef"
LEASES = "/var/lib/86box-server/dnsmasq.leases"


def golden(name):
    with open(os.path.join(GOLDEN, name)) as f:
        return f.read()


def seed_of(case):
    data = json.loads(golden(case + ".json"))
    problems, norm = core.validate(data, seed=True)
    assert not problems, problems
    return norm


def req(groups, **extra):
    r = {"id": RID, "netgroups": groups}
    r.update(extra)
    return r


LAB = {"lab": {"cidr": "10.86.0.0/24"}}
HOST = {"nets": [ipaddress.ip_network("216.128.128.0/23"), ipaddress.ip_network("169.254.169.254/32")],
        "devices": {"enp1s0": False, "lo": False}, "managed": set()}


class GoldenTest(unittest.TestCase):
    """The rendered files must equal what the old Ansible templates produced."""

    def test_live_lab_file_is_reproduced_byte_for_byte(self):
        n = seed_of("lab")
        self.assertEqual(core.render_nft(n["netgroups"], n["group_routes"]), golden("live-lab.nft"))

    def test_mixed_example_matches_the_templates(self):
        n = seed_of("mixed")
        self.assertEqual(core.render_nft(n["netgroups"], n["group_routes"]), golden("mixed.nft"))
        self.assertEqual(core.render_dnsmasq(n["netgroups"], n["group_routes"], n["nat_dns"], LEASES),
                         golden("mixed.dnsmasq"))
        self.assertEqual(core.render_nm([g["bridge"] for g in n["netgroups"].values()]), golden("mixed.nm"))

    def test_lab_dnsmasq_and_nm_match(self):
        n = seed_of("lab")
        self.assertEqual(core.render_dnsmasq(n["netgroups"], [], n["nat_dns"], LEASES), golden("lab.dnsmasq"))
        self.assertEqual(core.render_nm([g["bridge"] for g in n["netgroups"].values()]), golden("lab.nm"))

    def test_cloudflare_only_rule_matches(self):
        n = seed_of("lab")
        edge = {"edge_mode": "cf-origin", "cf4_ranges": ["173.245.48.0/20", "103.21.244.0/22"],
                "cf6_ranges": ["2400:cb00::/32"]}
        self.assertEqual(core.render_nft(n["netgroups"], [], edge), golden("cf.nft"))


class ValidateTest(unittest.TestCase):
    def check(self, r, host=None, seed=False):
        return core.validate(r, host, seed)

    def test_a_good_request_is_normalized(self):
        problems, n = self.check(req({"web": {"cidr": "10.86.1.0/24", "egress": "nat", "dhcp": True}, **LAB},
                                     group_routes=[["lab", "web"]]), HOST)
        self.assertEqual(problems, [])
        self.assertEqual(list(n["netgroups"]), ["lab", "web"])           # sorted by name
        self.assertEqual(n["netgroups"]["lab"], {"bridge": "br-lab", "cidr": "10.86.0.0/24", "egress": "none",
                                                 "egress_allow": [], "dhcp": False})
        self.assertEqual(n["nat_dns"], ["1.1.1.1"])
        self.assertEqual(n["group_routes"], [["lab", "web"]])

    def test_no_groups_is_fine(self):
        self.assertEqual(self.check(req({}), HOST)[0], [])

    def test_hostile_networks_are_refused(self):
        for cidr in ("127.0.0.0/8", "8.8.8.0/24", "216.128.128.0/23", "0.0.0.0/0", "10.0.0.0/8", "10.86.0.0/15",
                     "10.86.0.0/30", "169.254.0.0/16", "100.64.0.0/24", "11.0.0.0/24", "172.32.0.0/24",
                     "192.169.0.0/24", "fd00::/64", "10.86.0.5/24", "not-a-network", "", 5, None, ["10.1.0.0/24"]):
            problems, n = self.check(req({"x": {"cidr": cidr}}), HOST)
            self.assertTrue(problems, cidr)
            self.assertIsNone(n)

    def test_networks_in_use_on_the_host_are_refused(self):
        host = dict(HOST, nets=HOST["nets"] + [ipaddress.ip_network("10.200.0.0/16"),
                                               ipaddress.ip_network("172.17.0.0/16")])
        for cidr in ("10.200.5.0/24", "10.200.0.0/16", "172.17.4.0/24", "172.16.0.0/12"):
            self.assertTrue(self.check(req({"x": {"cidr": cidr}}), host)[0], cidr)
        self.assertEqual(self.check(req({"x": {"cidr": "10.201.0.0/24"}}), host)[0], [])

    def test_groups_may_not_overlap_or_share_a_bridge(self):
        problems, _ = self.check(req({"a": {"cidr": "10.1.0.0/24"}, "b": {"cidr": "10.1.0.128/25"}}))
        self.assertIn("a and b: their networks overlap", problems)
        problems, _ = self.check(req({"a": {"cidr": "10.1.0.0/24", "bridge": "br-x"},
                                      "b": {"cidr": "10.2.0.0/24", "bridge": "br-x"}}))
        self.assertTrue(any("same bridge" in p for p in problems))

    def test_names_and_bridges_are_strict(self):
        for name in ("Lab", "1lab", "a-b", "a" * 13, "", "a b", 'x"; drop', "../x"):
            self.assertTrue(self.check(req({name: {"cidr": "10.1.0.0/24"}}))[0], name)
        for bridge in ("lab", "br-", "br-Lab", "br-" + "a" * 13, 'br-a"b', "br-a b", "br-a;b", 7):
            self.assertTrue(self.check(req({"a": {"cidr": "10.1.0.0/24", "bridge": bridge}}))[0], bridge)
        self.assertEqual(self.check(req({"a": {"cidr": "10.1.0.0/24", "bridge": "br-custom"}}))[0], [])

    def test_unknown_keys_are_refused(self):
        self.assertTrue(self.check(req(LAB, extra=1))[0])
        self.assertTrue(self.check(req({"lab": {"cidr": "10.1.0.0/24", "uplink": "enp1s0"}}))[0])
        self.assertTrue(self.check(req(LAB, edge={"edge_mode": "internal"}))[0])      # edge is seed-only
        self.assertEqual(self.check(req(LAB, edge={"edge_mode": "internal"}), seed=True)[0], [])

    def test_egress_and_allow_list(self):
        self.assertTrue(self.check(req({"a": {"cidr": "10.1.0.0/24", "egress": "all"}}))[0])
        for bad in ("1.1.1.1 } drop; {", "1.1.1.1\n", "example.com", "::1", "1.1.1.1/33", "300.1.1.1", 4, "1.1.1.1;"):
            self.assertTrue(self.check(req({"a": {"cidr": "10.1.0.0/24", "egress": "nat", "egress_allow": [bad]}}))[0], bad)
        self.assertTrue(self.check(req({"a": {"cidr": "10.1.0.0/24", "egress_allow": "1.1.1.1"}}))[0])
        ok = {"a": {"cidr": "10.1.0.0/24", "egress": "nat", "egress_allow": ["1.1.1.1", "8.8.8.0/24"]}}
        self.assertEqual(self.check(req(ok))[0], [])

    def test_dhcp_must_be_a_boolean(self):
        for bad in ("true", 1, None, "no"):
            self.assertTrue(self.check(req({"a": {"cidr": "10.1.0.0/24", "dhcp": bad}}))[0], bad)

    def test_routes(self):
        both = {"a": {"cidr": "10.1.0.0/24"}, "b": {"cidr": "10.2.0.0/24"}}
        self.assertEqual(self.check(req(both, group_routes=[["a", "b"]]))[0], [])
        for bad in ([["a", "a"]], [["a", "zz"]], [["a"]], [["a", "b"], ["b", "a"]], ["ab"], [[1, 2]], "a,b"):
            self.assertTrue(self.check(req(both, group_routes=bad))[0], bad)

    def test_nat_dns(self):
        self.assertEqual(self.check(req(LAB, nat_dns=["9.9.9.9", "1.1.1.1"]))[0], [])
        for bad in ([], ["x"], ["1.1.1.1; rm"], ["::1"], "1.1.1.1", ["1.1.1.1"] * 4, [5]):
            self.assertTrue(self.check(req(LAB, nat_dns=bad))[0], bad)

    def test_request_id(self):
        for bad in (None, "", "xyz", "ABCDEF12", "a" * 65, 5, "1234567"):
            r = req(LAB)
            r["id"] = bad
            self.assertTrue(self.check(r)[0], bad)
        self.assertEqual(self.check({"netgroups": {}}, seed=True)[0], [])

    def test_too_many_groups(self):
        many = {"g%d" % i: {"cidr": "10.%d.0.0/24" % (i + 1)} for i in range(33)}
        self.assertTrue(any("at most" in p for p in self.check(req(many))[0]))
        self.assertEqual(self.check(req({k: many[k] for k in list(many)[:32]}))[0], [])

    def test_not_an_object(self):
        for bad in ([], "x", None, 5):
            self.assertTrue(self.check(bad)[0])
        self.assertTrue(self.check(req([]))[0])

    def test_bridge_name_clashes(self):
        host = dict(HOST, devices={"br-lab": True, "br-docker": True, "br-nic": False}, managed={"br-lab"})
        self.assertEqual(self.check(req(LAB), host)[0], [])                                   # ours: fine
        self.assertTrue(self.check(req({"docker": {"cidr": "10.9.0.0/24"}}), host)[0])        # exists, not ours
        self.assertTrue(self.check(req({"nic": {"cidr": "10.9.0.0/24"}}), host)[0])           # not a bridge at all
        self.assertEqual(self.check(req({"docker": {"cidr": "10.9.0.0/24"}}), host, seed=True)[0], [])
        self.assertTrue(self.check(req({"nic": {"cidr": "10.9.0.0/24"}}), host, seed=True)[0])

    def test_edge_settings_are_checked_for_the_seed(self):
        for bad in ({"edge_mode": "x"}, {"cf4_ranges": ["::1/128"]}, {"cf6_ranges": ["1.1.1.0/24"]}, {"nope": 1}, []):
            self.assertTrue(self.check(req({}, edge=bad), seed=True)[0], bad)


class NetworkManagerTest(unittest.TestCase):
    def test_a_kept_bridge_stays_on_the_ignore_list(self):
        self.assertEqual(core.render_nm(["br-lab", "br-web"]).splitlines()[-1],
                         "unmanaged-devices=interface-name:br-lab;interface-name:br-web;interface-name:tap*")


class HostTest(unittest.TestCase):
    ADDR = [{"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "enp1s0", "addr_info": [{"family": "inet", "local": "216.128.128.26", "prefixlen": 23},
                                               {"family": "inet6", "local": "fe80::1", "prefixlen": 64}]},
            {"ifname": "br-lab", "addr_info": [{"family": "inet", "local": "10.86.0.1", "prefixlen": 24}]},
            {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]}]
    ROUTES = [{"dst": "default", "dev": "enp1s0"}, {"dst": "10.86.0.0/24", "dev": "br-lab"},
              {"dst": "216.128.128.0/23", "dev": "enp1s0"}, {"dst": "169.254.169.254", "dev": "enp1s0"},
              {"dst": "fd00::/64", "dev": "enp1s0"}, {"dst": "172.17.0.0/16", "dev": "docker0"}]

    def test_our_own_bridges_are_left_out(self):
        nets = {str(n) for n in core.host_nets(self.ADDR, self.ROUTES, {"br-lab"})}
        self.assertEqual(nets, {"127.0.0.0/8", "216.128.128.0/23", "169.254.169.254/32", "172.17.0.0/16"})

    def test_everything_counts_when_nothing_is_ignored(self):
        nets = {str(n) for n in core.host_nets(self.ADDR, self.ROUTES)}
        self.assertIn("10.86.0.0/24", nets)

    def test_a_new_group_may_not_sit_on_docker_or_the_uplink(self):
        host = {"nets": core.host_nets(self.ADDR, self.ROUTES, {"br-lab"}), "devices": {}, "managed": {"br-lab"}}
        self.assertTrue(core.validate(req({"x": {"cidr": "172.17.5.0/24"}}), host)[0])
        self.assertEqual(core.validate(req(LAB), host)[0], [])           # keeping lab as it is is fine


class HelperTest(unittest.TestCase):
    def test_dhcp_pool(self):
        self.assertEqual(core.dhcp_pool("10.86.0.0/24"), {"first": "10.86.0.100", "last": "10.86.0.199",
                                                         "netmask": "255.255.255.0", "router": "10.86.0.1"})
        self.assertEqual(core.dhcp_pool("172.20.0.0/28")["first"], "172.20.0.8")
        self.assertEqual(core.dhcp_pool("10.0.0.0/16")["first"], "10.0.0.100")

    def test_host_address(self):
        self.assertEqual(core.host_address("10.86.1.0/24"), "10.86.1.1/24")

    def test_forwarding_is_needed_for_nat_or_routes_only(self):
        n = seed_of("lab")
        self.assertFalse(core.needs_forwarding(n["netgroups"], []))
        self.assertTrue(core.needs_forwarding(n["netgroups"], [["lab", "web"]]))
        self.assertTrue(core.needs_forwarding(seed_of("mixed")["netgroups"], []))


class UfwTest(unittest.TestCase):
    def test_rules_for_the_mixed_example(self):
        n = seed_of("mixed")
        rules = core.desired_ufw(n["netgroups"], n["group_routes"], "enp1s0")
        self.assertIn(["allow", "in", "on", "br-lab", "proto", "udp", "to", "any", "port", "67"], rules)
        self.assertIn(["route", "allow", "in", "on", "br-web", "out", "on", "enp1s0"], rules)
        self.assertIn(["route", "allow", "in", "on", "br-lab", "out", "on", "br-web"], rules)
        self.assertIn(["route", "allow", "in", "on", "br-web", "out", "on", "br-lab"], rules)
        self.assertNotIn(["route", "allow", "in", "on", "br-lab", "out", "on", "enp1s0"], rules)   # lab has no egress
        self.assertEqual(len(rules), len({tuple(r) for r in rules}))

    def test_nothing_needed_for_an_isolated_group(self):
        n = seed_of("lab")
        self.assertEqual(core.desired_ufw(n["netgroups"], [], "enp1s0"), [])

    def test_no_uplink_means_no_nat_rules(self):
        n = seed_of("mixed")
        rules = core.desired_ufw(n["netgroups"], [], None)
        self.assertTrue(all(r[0] != "route" for r in rules))

    def test_diff_and_delete_arguments(self):
        old = [["allow", "in", "on", "br-a", "proto", "udp", "to", "any", "port", "67"],
               ["route", "allow", "in", "on", "br-a", "out", "on", "enp1s0"]]
        new = [["route", "allow", "in", "on", "br-a", "out", "on", "enp1s0"],
               ["route", "allow", "in", "on", "br-b", "out", "on", "enp1s0"]]
        add, delete = core.ufw_diff(old, new)
        self.assertEqual(add, [new[1]])
        self.assertEqual(delete, [old[0]])
        self.assertEqual(core.ufw_delete_args(old[0]), ["delete", "allow", "in", "on", "br-a", "proto", "udp", "to", "any", "port", "67"])
        self.assertEqual(core.ufw_delete_args(old[1]), ["route", "delete", "allow", "in", "on", "br-a", "out", "on", "enp1s0"])
        self.assertEqual(core.ufw_diff(new, new), ([], []))


if __name__ == "__main__":
    unittest.main()
