"""Shared test setup for clone/import tests: a temp data root and a fake auth."""
import json
import os
import tempfile
import unittest
from unittest import mock

import config
import vms

ADMIN = {"name": "admin", "role": "admin", "vms": []}


class FakeAuth:
    def __init__(self):
        self.users = {"bob": {"vms": ["src"]}}
        self.calls = []

    def console_users(self):
        return self.users

    def set_vm_users(self, vm, names):
        self.calls.append(("set", vm, list(names)))

    def remove_vm(self, vm):
        self.calls.append(("remove", vm))

    def can_use_vm(self, user, vm):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for d in ("vmdefs", "vms", "media/isos", "vnc-tokens", "manager"):
            os.makedirs(os.path.join(self.root, d))
        self.old = dict(config.CFG)
        config.CFG.update(data_root=self.root, emu86box_tag="v6.0",
                          netgroups={"lab": {"bridge": "br-lab"}, "web": {"bridge": "br-web"}})
        self.auth = FakeAuth()
        self.active = set()
        for p in (mock.patch.object(vms, "_auth", return_value=self.auth),
                  mock.patch.object(vms.units, "is_active", side_effect=lambda u: u in self.active)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        config.CFG.clear()
        config.CFG.update(self.old)
        self.tmp.cleanup()

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def make_vm(self, name="src", cfg=None, display=10, groups=("lab",)):
        d = self.p("vms", name)
        os.makedirs(os.path.join(d, "disks"))
        os.makedirs(os.path.join(d, "snapshots", "old"))
        with open(os.path.join(d, "86box.cfg"), "w") as f:
            f.write(cfg if cfg is not None else
                    "[Machine]\nmachine = p65up5_cp6nd\n\n[NE2000 Compatible #1]\nmac = aa:bb:cc\n\n"
                    "[Network]\nnet_01_net_type = tap\nnet_01_host_device = br-lab\n\n"
                    "[Hard disks]\nhdd_01_parameters = 63, 16, 100, 0, ide\nhdd_01_fn = disks/hdd1.img\n")
        with open(os.path.join(d, "disks", "hdd1.img"), "wb") as f:
            f.truncate(63 * 16 * 100 * 512)
            f.seek(0)
            f.write(b"DATA")
        with open(os.path.join(d, "86box.log"), "w") as f:
            f.write("log")
        with open(self.p("vmdefs", name + ".json"), "w") as f:
            json.dump({"name": name, "display": display, "allowed_groups": list(groups), "autostart": True,
                       "template": "ppro200"}, f)
        return d
