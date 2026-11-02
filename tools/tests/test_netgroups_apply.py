"""Tests for the parts of netgroups-apply that need no root (the checks that run before anything changes)."""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "..", "..", "ansible", "roles", "netgroups", "files")
loader = importlib.machinery.SourceFileLoader("netgroups_apply", os.path.join(FILES, "netgroups-apply"))
spec = importlib.util.spec_from_loader("netgroups_apply", loader)
na = importlib.util.module_from_spec(spec)
loader.exec_module(na)

HOST = {"nets": [], "uplink": "enp1s0", "managed": set(), "devices": {}}
STATE = {"groups": {}, "routes": [], "dns": ["1.1.1.1"], "edge": {}, "ufw": [], "orphans": []}


class ProcessTest(unittest.TestCase):
    def run_process(self, data, seed=False):
        ctx = na.Ctx("/opt/86server/data", "86server")
        steps = {n: mock.DEFAULT for n in ("apply_all",)}
        with mock.patch.object(na, "load_state", return_value=STATE), \
                mock.patch.object(na, "host_info", return_value=HOST), \
                mock.patch.object(na, "apply_all") as apply_all:
            result = na.process(ctx, data, seed)
        return result, apply_all, ctx

    def test_a_refused_request_changes_nothing(self):
        (ok, message), apply_all, _ = self.run_process({"id": "0123456789abcdef", "netgroups": {"x": {"cidr": "8.8.8.0/24"}}})
        self.assertFalse(ok)
        self.assertIn("Refused, nothing was changed", message)
        apply_all.assert_not_called()

    def test_a_good_request_is_applied(self):
        (ok, message), apply_all, ctx = self.run_process({"id": "0123456789abcdef", "netgroups": {"lab": {"cidr": "10.86.0.0/24"}}})
        self.assertTrue(ok)
        apply_all.assert_called_once()
        self.assertEqual(apply_all.call_args[0][1]["netgroups"]["lab"]["bridge"], "br-lab")
        self.assertIn("Nothing to change", message)          # the mock changed nothing

    def test_an_error_while_applying_is_reported_not_raised(self):
        ctx = na.Ctx("/x", "g")
        with mock.patch.object(na, "load_state", return_value=STATE), mock.patch.object(na, "host_info", return_value=HOST), \
                mock.patch.object(na, "apply_all", side_effect=na.ApplyError("ip failed")):
            ok, message = na.process(ctx, {"id": "0123456789abcdef", "netgroups": {}}, False)
        self.assertFalse(ok)
        self.assertIn("ip failed", message)

    def test_seed_may_carry_edge_settings(self):
        (ok, _), apply_all, _ = self.run_process({"netgroups": {}, "edge": {"edge_mode": "acme"}}, seed=True)
        self.assertTrue(ok)
        self.assertEqual(apply_all.call_args[0][1]["edge"], {"edge_mode": "acme", "cf4_ranges": [], "cf6_ranges": []})


class InputTest(unittest.TestCase):
    def test_symlinks_and_big_files_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real.json")
            open(real, "w").write("{}")
            link = os.path.join(d, "link.json")
            os.symlink(real, link)
            self.assertEqual(na.read_input(real), {})
            with self.assertRaises(OSError):
                na.read_input(link)
            big = os.path.join(d, "big.json")
            open(big, "w").write(" " * (na.MAX_REQUEST + 1))
            with self.assertRaises(ValueError):
                na.read_input(big)

    def test_bad_json_is_a_value_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            open(p, "w").write("{not json")
            with self.assertRaises(ValueError):
                na.read_input(p)


class SystemFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.etc = os.path.join(self.tmp.name, "etc")
        self.sys = os.path.join(self.tmp.name, "opt", "system", "etc")
        self.p = mock.patch.multiple(na, ETC=self.etc, SYSTEM_ETC=self.sys)
        self.p.start()
        self.chown = mock.patch.object(na.os, "chown")  # the helper runs as root; tests don't
        self.chown.start()

    def tearDown(self):
        self.chown.stop()
        self.p.stop()
        self.tmp.cleanup()

    def test_real_file_in_opt_and_symlink_in_etc(self):
        link = os.path.join(self.etc, "sysctl.d", "86box.conf")
        self.assertTrue(na.write_system_file(link, "x\n", 0o644))
        real = os.path.join(self.sys, "sysctl.d", "86box.conf")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(real))
        self.assertFalse(os.path.islink(real))
        self.assertFalse(na.write_system_file(link, "x\n", 0o644))  # nothing changed

    def test_a_plain_file_in_etc_is_replaced_by_the_link(self):
        link = os.path.join(self.etc, "sysctl.d", "86box.conf")
        os.makedirs(os.path.dirname(link))
        with open(link, "w") as f:
            f.write("old\n")
        self.assertTrue(na.write_system_file(link, "new\n", 0o644))
        self.assertTrue(os.path.islink(link))
        with open(link) as f:
            self.assertEqual(f.read(), "new\n")

    def test_remove_drops_both(self):
        link = os.path.join(self.etc, "NetworkManager", "conf.d", "86box.conf")
        na.write_system_file(link, "x\n", 0o644)
        self.assertTrue(na.system_file_exists(link))
        na.remove_system_file(link)
        self.assertFalse(os.path.lexists(link))
        self.assertFalse(na.system_file_exists(link))


if __name__ == "__main__":
    unittest.main()
