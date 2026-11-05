"""cfg-guard: which network settings a box may run with."""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "..", "..", "ansible", "roles", "vms", "files", "cfg-guard")
loader = importlib.machinery.SourceFileLoader("cfg_guard", PATH)
spec = importlib.util.spec_from_loader("cfg_guard", loader)
cg = importlib.util.module_from_spec(spec)
loader.exec_module(cg)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        os.makedirs(os.path.join(root, "boxdefs"))
        os.makedirs(os.path.join(root, "boxes", "ws1"))
        with open(os.path.join(root, "boxdefs", "ws1.json"), "w") as f:
            json.dump({"name": "ws1", "allowed_groups": ["ws"]}, f)
        groups = os.path.join(root, "netgroups.json")
        with open(groups, "w") as f:
            json.dump({"ws": {"bridge": "br-ws"}, "lab": {"bridge": "br-lab"}}, f)
        for p in (mock.patch.object(cg, "ROOT", root), mock.patch.object(cg, "NETGROUPS", groups)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, network):
        with open(os.path.join(self.tmp.name, "boxes", "ws1", "86box.cfg"), "w") as f:
            f.write("[Machine]\nmachine = p65up5_cp6nd\n\n[Network]\n" + network)
        return cg.check("ws1")

    def test_tap_without_a_card_is_fine(self):
        # 86Box Settings with the card left at None but the mode set to TAP (seen live on ws1).
        self.assertEqual(self.check("net_01_net_type = tap\n"), [])
        self.assertEqual(self.check("net_01_net_type = slirp\nnet_01_card = none\n"), [])

    def test_card_on_an_allowed_bridge(self):
        self.assertEqual(self.check("net_01_card = ne2k\nnet_01_net_type = tap\nnet_01_host_device = br-ws\n"), [])

    def test_card_without_a_bridge(self):
        p = self.check("net_01_card = ne2k\nnet_01_net_type = tap\n")
        self.assertEqual(len(p), 1)
        self.assertIn("(none picked)", p[0])
        self.assertIn("allowed: br-ws", p[0])

    def test_other_groups_bridge_and_slirp_refused(self):
        self.assertIn("br-lab", self.check("net_01_card = ne2k\nnet_01_net_type = tap\nnet_01_host_device = br-lab\n")[0])
        self.assertIn("only tap", self.check("net_01_card = ne2k\nnet_01_net_type = slirp\n")[0])



TAP = os.path.join(HERE, "..", "..", "ansible", "roles", "vms", "files", "tap-check")
tl = importlib.machinery.SourceFileLoader("tap_check", TAP)
tc = importlib.util.module_from_spec(importlib.util.spec_from_loader("tap_check", tl))
tl.exec_module(tc)


class TapCheckTest(unittest.TestCase):
    def bridges(self, network):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "boxes", "ws1"))
            with open(os.path.join(root, "boxes", "ws1", "86box.cfg"), "w") as f:
                f.write("[Network]\n" + network)
            with mock.patch.object(tc, "ROOT", root):
                return tc.expected_bridges("ws1")

    def test_only_slots_with_a_card_need_a_tap(self):
        self.assertEqual(self.bridges("net_01_net_type = tap\n"), [])
        self.assertEqual(self.bridges("net_01_card = ne2k\nnet_01_net_type = tap\nnet_01_host_device = br-ws\n"
                                      "net_02_net_type = tap\n"), ["br-ws"])


if __name__ == "__main__":
    unittest.main()
