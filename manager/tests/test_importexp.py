import json
import os
import unittest

import importexp
import vms
from tests._vmfixture import Base


class ImportExportTest(Base):
    def exported(self, **kw):
        self.make_vm(**kw)
        return importexp.export_vm("src")

    def test_export_shape(self):
        data = self.exported()
        self.assertEqual(data["format"], 1)
        self.assertNotIn("display", data["vm"])
        self.assertIn("machine = p65up5_cp6nd", data["cfg"])

    def test_round_trip(self):
        data = self.exported()
        importexp.import_vm("copy1", json.loads(json.dumps(data)), ["lab"], True, ["bob"])
        self.assertEqual(os.path.getsize(self.p("vms/copy1/disks/hdd1.img")), 63 * 16 * 100 * 512)
        with open(self.p("vms/copy1/disks/hdd1.img"), "rb") as f:
            self.assertEqual(f.read(4), b"\0\0\0\0")
        d = json.load(open(self.p("vmdefs/copy1.json")))
        self.assertEqual((d["display"], d["allowed_groups"], d["autostart"], d["template"]), (11, ["lab"], True, "ppro200"))
        self.assertIn("machine = p65up5_cp6nd", open(self.p("vms/copy1/86box.cfg")).read())
        self.assertTrue(os.path.exists(self.p("vnc-tokens/copy1")))
        self.assertIn(("set", "copy1", ["bob"]), self.auth.calls)

    def test_slirp_refused(self):
        data = self.exported(cfg="[Network]\nnet_01_net_type = slirp\n")
        with self.assertRaisesRegex(vms.VmError, "only tap is allowed"):
            importexp.import_vm("copy1", data, ["lab"], False, [])
        self.assertFalse(os.path.exists(self.p("vms/copy1")))

    def test_bridge_must_be_in_chosen_groups(self):
        data = self.exported()
        with self.assertRaisesRegex(vms.VmError, "bridge"):
            importexp.import_vm("copy1", data, ["web"], False, [])
        with self.assertRaisesRegex(vms.VmError, "bridge"):
            importexp.import_vm("copy1", data, [], False, [])

    def test_paths_outside_refused(self):
        for line in ("hdd_02_fn = /etc/passwd", "hdd_02_fn = ../other/disks/hdd1.img",
                     "cdrom_01_image_path = /var/lib/x/y.iso"):
            data = self.exported(name="src") if not os.path.exists(self.p("vms/src")) else importexp.export_vm("src")
            data["cfg"] += "\n[Hard disks]\n" + line + "\n" if line.startswith("hdd") else "\n[Floppy and CD-ROM drives]\n" + line + "\n"
            with self.assertRaises(vms.VmError):
                importexp.import_vm("copy1", data, ["lab"], False, [])
        data = importexp.export_vm("src")
        data["cfg"] += "\n[Floppy and CD-ROM drives]\ncdrom_01_image_path = %s\n" % self.p("media/isos/os.iso")
        importexp.import_vm("copy2", data, ["lab"], False, [])

    def test_bad_files(self):
        self.make_vm()
        for bad in ({}, {"format": 2, "vm": {}, "cfg": ""}, {"format": 1, "vm": {}, "cfg": 5}, [1], "x"):
            with self.assertRaisesRegex(vms.VmError, "not a VM export"):
                importexp.import_vm("copy1", bad, [], False, [])
        with self.assertRaisesRegex(vms.VmError, "parse"):
            importexp.import_vm("copy1", {"format": 1, "vm": {}, "cfg": "no section\nx=1"}, [], False, [])

    def test_name_taken_and_bad_disk(self):
        data = self.exported()
        with self.assertRaisesRegex(vms.VmError, "already exists"):
            importexp.import_vm("src", data, ["lab"], False, [])
        data["cfg"] = data["cfg"].replace("63, 16, 100, 0, ide", "63, 16, 100, 0, scsi")
        with self.assertRaisesRegex(vms.VmError, "IDE"):
            importexp.import_vm("copy1", data, ["lab"], False, [])
        self.assertFalse(os.path.exists(self.p("vms/copy1")))

    def test_empty_cfg_imports_without_disk(self):
        importexp.import_vm("copy1", {"format": 1, "vm": {}, "cfg": ""}, [], False, [])
        self.assertFalse(os.path.exists(self.p("vms/copy1/disks/hdd1.img")))


if __name__ == "__main__":
    unittest.main()
