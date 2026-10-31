import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import config
import snapshots


def rd(path):
    with open(path, "rb") as f:
        return f.read()


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        config.CFG["data_root"] = self.root
        self.vm = os.path.join(self.root, "boxes/vm1")
        for d in ("manager", "boxdefs", "boxes/vm1/disks", "boxes/vm1/nvr"):
            os.makedirs(os.path.join(self.root, d))
        with open(os.path.join(self.root, "boxdefs/vm1.json"), "w") as f:
            json.dump({"name": "vm1"}, f)
        self.write("86box.cfg", b"cfg1")
        self.write("disks/hdd1.img", b"disk1")
        self.write("nvr/a.nvr", b"nvr1")
        for p in (mock.patch("units.is_active", return_value=False),
                  mock.patch("snapshots.shutil.disk_usage", return_value=SimpleNamespace(free=10 * 1024 ** 3))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, data):
        with open(os.path.join(self.vm, rel), "wb") as f:
            f.write(data)

    def test_create_list_contents(self):
        err, snap = snapshots.create("vm1", "first")
        self.assertIsNone(err)
        self.assertTrue(snap.endswith("-first"))
        d = os.path.join(self.vm, "snapshots", snap)
        self.assertEqual(sorted(os.listdir(d)), ["86box.cfg", "disks", "nvr", "vmdef.json"])
        self.assertEqual(rd(os.path.join(d, "disks/hdd1.img")), b"disk1")
        self.assertEqual([s["name"] for s in snapshots.list_snapshots("vm1")], [snap])

    def test_running_vm_refused(self):
        with mock.patch("units.is_active", return_value=True):
            self.assertEqual(snapshots.create("vm1")[0], "Stop the box first.")
            self.assertEqual(snapshots.restore("vm1", "20200101-000000"), "Stop the box first.")

    def test_bad_label_and_names(self):
        self.assertIn("Label", snapshots.create("vm1", "Bad Label")[0])
        self.assertEqual(snapshots.create("../x")[0], "bad box name")
        self.assertEqual(snapshots.restore("vm1", "../../etc"), "no such snapshot")

    def test_no_space(self):
        with mock.patch("snapshots.shutil.disk_usage", return_value=SimpleNamespace(free=10)):
            self.assertIn("free disk space", snapshots.create("vm1")[0])

    def test_restore_brings_back_and_saves_before(self):
        _, snap = snapshots.create("vm1")
        self.write("disks/hdd1.img", b"CHANGED")
        os.unlink(os.path.join(self.vm, "nvr/a.nvr"))
        self.assertIsNone(snapshots.restore("vm1", snap))
        self.assertEqual(rd(os.path.join(self.vm, "disks/hdd1.img")), b"disk1")
        self.assertEqual(rd(os.path.join(self.vm, "nvr/a.nvr")), b"nvr1")
        names = [s["name"] for s in snapshots.list_snapshots("vm1")]
        before = [n for n in names if n.endswith("-before-restore")]
        self.assertEqual(len(before), 1)
        self.assertEqual(rd(os.path.join(self.vm, "snapshots", before[0], "disks/hdd1.img")), b"CHANGED")

    def test_vmdef_not_restored(self):
        _, snap = snapshots.create("vm1")
        with open(os.path.join(self.root, "boxdefs/vm1.json"), "w") as f:
            json.dump({"name": "vm1", "display": 99}, f)
        snapshots.restore("vm1", snap)
        self.assertEqual(json.load(open(os.path.join(self.root, "boxdefs/vm1.json")))["display"], 99)

    def test_delete_needs_confirm(self):
        _, snap = snapshots.create("vm1")
        self.assertIn("Type", snapshots.delete("vm1", snap, "nope"))
        self.assertIsNone(snapshots.delete("vm1", snap, snap))
        self.assertEqual(snapshots.list_snapshots("vm1"), [])

    def test_http_create_json_and_section(self):
        req = SimpleNamespace(form=lambda: {"label": "x"}, headers={"Accept": "application/json"},
                              user={"name": "admin"}, session={"csrf": "tok"}, ip="1.1.1.1")
        m = SimpleNamespace(group=lambda i: "vm1")
        status, _, body = snapshots.h_create(req, m)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        html = snapshots.html_section(req, "vm1")
        self.assertIn("Take snapshot", html)
        self.assertIn('value="tok"', html)


if __name__ == "__main__":
    unittest.main()
