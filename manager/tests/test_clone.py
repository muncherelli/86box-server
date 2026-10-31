import configparser
import json
import os
import unittest

import clone
import vms
from tests._vmfixture import ADMIN, Base


def macs(path):
    p = configparser.ConfigParser(interpolation=None)
    p.optionxform = str
    p.read(path)
    return p.get("NE2000 Compatible #1", "mac")


class CloneTest(Base):
    def test_clone_copies_and_changes_mac(self):
        self.make_vm()
        clone.clone_vm("src", "copy1")
        self.assertNotEqual(macs(self.p("vms/copy1/86box.cfg")), "aa:bb:cc")
        self.assertRegex(macs(self.p("vms/copy1/86box.cfg")), r"^[0-9a-f]{2}(:[0-9a-f]{2}){2}$")
        self.assertEqual(macs(self.p("vms/src/86box.cfg")), "aa:bb:cc")
        with open(self.p("vms/copy1/disks/hdd1.img"), "rb") as f:
            self.assertEqual(f.read(4), b"DATA")
        self.assertEqual(os.listdir(self.p("vms/copy1/snapshots")), [])
        self.assertFalse(os.path.exists(self.p("vms/copy1/86box.log")))
        self.assertFalse(os.path.exists(self.p("vms/.new-copy1")))

    def test_record_display_token_and_users(self):
        self.make_vm()
        clone.clone_vm("src", "copy1")
        d = json.load(open(self.p("vmdefs/copy1.json")))
        self.assertEqual((d["display"], d["allowed_groups"], d["autostart"]), (11, ["lab"], False))
        self.assertIn("DISPLAY=:11", open(self.p("vmdefs/copy1.env")).read())
        self.assertEqual(open(self.p("vnc-tokens/copy1")).read(), "copy1: 127.0.0.1:5911\n")
        self.assertIn(("set", "copy1", ["bob"]), self.auth.calls)

    def test_running_source_refused(self):
        self.make_vm()
        self.active.add("vm@src.service")
        with self.assertRaisesRegex(vms.VmError, "Stop the box"):
            clone.clone_vm("src", "copy1")
        self.assertFalse(os.path.exists(self.p("vms/copy1")))

    def test_bad_and_taken_names(self):
        self.make_vm()
        self.make_vm("other", display=11)
        for bad in ("", "X", "../x", "new", "other"):
            with self.assertRaises(vms.VmError):
                clone.clone_vm("src", bad)
        with self.assertRaisesRegex(vms.VmError, "No such box"):
            clone.clone_vm("ghost", "copy1")

    def test_failure_leaves_nothing(self):
        self.make_vm()
        from unittest import mock
        with mock.patch.object(vms, "_write_token", side_effect=OSError("boom")):
            with self.assertRaises(vms.VmError):
                clone.clone_vm("src", "copy1")
        self.assertEqual(sorted(os.listdir(self.p("vms"))), ["src"])
        self.assertEqual(sorted(os.listdir(self.p("vmdefs"))), ["src.json"])
        self.assertEqual(os.listdir(self.p("vnc-tokens")), [])

    def test_route_is_admin_only(self):
        self.assertEqual([r[3] for r in clone.ROUTES], ["admin"])


if __name__ == "__main__":
    unittest.main()
