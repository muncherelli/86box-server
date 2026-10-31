import configparser
import json
import os
import re
import stat
import tempfile
import unittest
from unittest import mock

import config
import vms
import vms_pages
import store


class FakeAuth:
    def __init__(self):
        self.users = {"bob": {"vms": []}}
        self.calls = []

    def console_users(self):
        return self.users

    def set_vm_users(self, vm, names):
        self.calls.append(("set", vm, list(names)))

    def remove_vm(self, vm):
        self.calls.append(("remove", vm))

    def can_use_vm(self, user, vm):
        return user["role"] == "admin" or vm in user["vms"]


class Req:
    def __init__(self, user, body=b"", ip="127.0.0.1"):
        self.user, self.ip = user, ip
        self.session = {"csrf": "tok"}
        self._body = body

    def body(self):
        return self._body


def rd(path):
    with open(path) as f:
        return f.read()


ADMIN = {"name": "admin", "role": "admin", "vms": []}


class VmsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for d in ("boxdefs", "boxes", "media/isos", "vnc-tokens", "manager"):
            os.makedirs(os.path.join(self.root, d))
        self.old = dict(config.CFG)
        config.CFG.update(data_root=self.root, emu86box_tag="v6.0",
                          netgroups={"lab": {"bridge": "br-lab"}, "web": {"bridge": "br-web"}})
        self.auth = FakeAuth()
        self.active = set()
        patches = [mock.patch.object(vms, "_auth", return_value=self.auth),
                   mock.patch.object(vms.units, "is_active", side_effect=lambda u: u in self.active),
                   mock.patch.object(vms.units, "run", return_value=(True, ""))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with open(os.path.join(self.root, "media/isos/os.iso"), "wb") as f:
            f.write(b"x")

    def tearDown(self):
        config.CFG.clear()
        config.CFG.update(self.old)
        self.tmp.cleanup()

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def make(self, name="test2", **kw):
        args = dict(template="ppro200", disk_gb="", cd="", groups=[], autostart=False, users=[])
        args.update(kw)
        return vms.create_vm(name, **args)

    def test_create(self):
        self.make("test2", disk_gb="2", cd="os.iso", groups=["lab"], users=["bob"])
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.optionxform = str
        cfg.read(self.p("boxes/test2/86box.cfg"))
        self.assertEqual(cfg["Hard disks"]["hdd_01_parameters"], "63, 16, 4161, 0, ide")
        self.assertEqual(cfg["Hard disks"]["hdd_01_fn"], "disks/hdd1.img")
        self.assertEqual(cfg["Machine"]["machine"], "p65up5_cp6nd")
        self.assertEqual(cfg["Floppy and CD-ROM drives"]["cdrom_01_image_path"], self.p("media/isos/os.iso"))
        self.assertEqual(os.path.getsize(self.p("boxes/test2/disks/hdd1.img")), 2147475456)
        self.assertEqual(stat.S_IMODE(os.stat(self.p("boxes/test2/86box.cfg")).st_mode), 0o660)
        self.assertEqual(stat.S_IMODE(os.stat(self.p("boxes/test2")).st_mode), 0o2770)
        for sub in ("disks", "floppies", "snapshots"):
            self.assertTrue(os.path.isdir(self.p("boxes/test2", sub)))
        d = json.loads(rd(self.p("boxdefs/test2.json")))
        self.assertEqual((d["display"], d["allowed_groups"], d["autostart"]), (10, ["lab"], False))
        self.assertEqual(rd(self.p("boxdefs/test2.env")), "B86_DISPLAY=10\nB86_VNC_PORT=5910\nDISPLAY=:10\nB86_GEOMETRY=1024x768\n")
        self.assertEqual(rd(self.p("vnc-tokens/test2")), "test2: 127.0.0.1:5910\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.p("vnc-tokens/test2")).st_mode), 0o600)
        self.assertEqual(self.auth.calls, [("set", "test2", ["bob"])])

    def test_display_is_unique(self):
        self.make("aa")
        self.make("bb")
        self.assertEqual(vms.all_defs()["bb"]["display"], 11)

    def test_validation(self):
        for bad in ("A", "x", "9a", "new", "a_b", "a" * 40):
            with self.assertRaises(vms.VmError):
                self.make(bad)
        with self.assertRaises(vms.VmError):
            self.make("okname", disk_gb="9")
        with self.assertRaises(vms.VmError):
            self.make("okname", groups=["nope"])
        with self.assertRaises(vms.VmError):
            self.make("okname", users=["nobody"])
        self.make("okname")
        with self.assertRaises(vms.VmError):
            self.make("okname")

    def test_cd_symlink_and_bad_names_refused(self):
        os.symlink("/etc/hostname", self.p("media/isos/link.iso"))
        for cd in ("link.iso", "../x.iso", "missing.iso"):
            with self.assertRaises(vms.VmError):
                self.make("okname", cd=cd)

    def test_failure_at_the_last_step_leaves_nothing(self):
        with mock.patch.object(vms, "_write_token", side_effect=OSError("boom")):
            with self.assertRaises(vms.VmError):
                self.make("test3", users=["bob"])
        self.assertEqual(os.listdir(self.p("boxes")), [])
        self.assertEqual(os.listdir(self.p("boxdefs")), [])
        self.assertEqual(os.listdir(self.p("vnc-tokens")), [])
        self.assertEqual(self.auth.calls, [])

    def test_failure_after_users_set_is_undone(self):
        self.auth.set_vm_users = mock.Mock(side_effect=OSError("boom"))
        with self.assertRaises(vms.VmError):
            self.make("test3", users=["bob"])
        self.assertEqual(os.listdir(self.p("boxes")), [])
        self.assertEqual(os.listdir(self.p("boxdefs")), [])
        self.assertIn(("remove", "test3"), self.auth.calls)

    def test_delete_moves_to_trash(self):
        self.make("test2")
        self.active.add("box@test2.service")
        with self.assertRaises(vms.VmError):
            vms.delete_vm("test2")
        self.active.clear()
        vms.delete_vm("test2")
        self.assertFalse(os.path.exists(self.p("boxes/test2")))
        self.assertEqual(os.listdir(self.p("boxdefs")), [])
        self.assertEqual(os.listdir(self.p("vnc-tokens")), [])
        trash = os.listdir(self.p("trash"))
        self.assertEqual(len(trash), 1)
        self.assertTrue(re.fullmatch(r"test2-\d{8}-\d{6}", trash[0]))
        self.assertIn(("remove", "test2"), self.auth.calls)
        self.assertGreater(vms.trash_size(), 0)
        vms.empty_trash()
        self.assertEqual(os.listdir(self.p("trash")), [])

    def test_edit_and_cd_change(self):
        self.make("test2")
        vms.edit_vm("test2", ["web"], True, ["bob"], cd="os.iso")
        d = json.loads(rd(self.p("boxdefs/test2.json")))
        self.assertEqual((d["allowed_groups"], d["autostart"], d["template"]), (["web"], True, "ppro200"))
        self.assertIn("os.iso", rd(self.p("boxes/test2/86box.cfg")))
        vms.edit_vm("test2", ["web"], True, [], cd="")
        self.assertNotIn("cdrom_01_image_path", rd(self.p("boxes/test2/86box.cfg")))
        self.active.add("box@test2.service")
        with self.assertRaises(vms.VmError):
            vms.edit_vm("test2", [], False, [], cd="os.iso")
        vms.edit_vm("test2", [], False, [], cd=None)  # busy is fine when the CD is left alone

    def test_parse_resolution(self):
        for blank in ("", "auto", " AUTO "):
            self.assertEqual(vms.parse_resolution(blank), "auto")
        self.assertEqual(vms.parse_resolution("1280X800"), "1280x800")
        self.assertEqual(vms.parse_resolution("640x400"), "640x400")
        self.assertEqual(vms.parse_resolution("2560x1600"), "2560x1600")
        for preset in vms.RESOLUTION_PRESETS:
            self.assertEqual(vms.parse_resolution(preset), preset)
        self.assertEqual(len(vms.RESOLUTION_PRESETS), 16)
        for bad in ("639x400", "640x399", "2561x1600", "2560x1601", "1024", "1024x", "axb", "1024x768x2", "-1024x768"):
            with self.assertRaises(vms.VmError):
                vms.parse_resolution(bad)

    def test_resolution_is_saved_and_written_to_the_env_file(self):
        self.make("test2")
        self.assertIn("B86_GEOMETRY=1024x768\n", rd(self.p("boxdefs/test2.env")))
        vms.edit_vm("test2", [], False, [], resolution="1280x1024")
        d = json.loads(rd(self.p("boxdefs/test2.json")))
        env = rd(self.p("boxdefs/test2.env"))
        self.assertEqual(d["resolution"], "1280x1024")
        self.assertIn("B86_GEOMETRY=1280x1024\n", env)
        self.assertIn("B86_DISPLAY=%d\n" % d["display"], env)
        self.assertIn("resize=scale", vms.console_url("test2"))
        vms.edit_vm("test2", [], False, [], resolution=None)          # left alone
        self.assertEqual(json.loads(rd(self.p("boxdefs/test2.json")))["resolution"], "1280x1024")
        vms.edit_vm("test2", [], False, [], resolution="auto")        # back to following the browser
        self.assertIn("B86_GEOMETRY=1024x768\n", rd(self.p("boxdefs/test2.env")))
        self.assertIn("resize=remote", vms.console_url("test2"))
        before = rd(self.p("boxdefs/test2.env"))
        with self.assertRaises(vms.VmError):
            vms.edit_vm("test2", [], False, [], resolution="9999x9999")
        self.assertEqual(rd(self.p("boxdefs/test2.env")), before)      # a refused size changes nothing

    def test_console_url_defaults_to_following_the_browser(self):
        url = vms.console_url("nope")
        self.assertIn("resize=remote", url)
        self.assertIn("path=%2Fwebsockify%3Ftoken%3Dnope", url)

    def test_actions_and_permissions(self):
        self.make("test2")
        user = {"name": "bob", "role": "console", "vms": []}
        m = re.fullmatch(vms_pages.ROUTES[-1][1], "/manage/api/box/test2/reset")
        self.assertEqual(vms_pages.h_api_action(Req(user), m)[0], 403)
        user["vms"] = ["test2"]
        self.assertEqual(vms_pages.h_api_action(Req(user), m)[0], 200)
        vms.units.run.assert_called_with("restart", "box@test2.service")
        m = re.fullmatch(vms_pages.ROUTES[-1][1], "/manage/api/box/test2/stop")
        vms_pages.h_api_action(Req(user), m)
        vms.units.run.assert_called_with("stop", "box@test2.service")

    def test_list_hides_other_vms_and_shows_state(self):
        self.make("aa")
        self.make("bb")
        self.active.add("box@aa.service")
        user = {"name": "bob", "role": "console", "vms": ["aa"]}
        body = json.loads(vms_pages.h_api_list(Req(user), None)[2])
        self.assertEqual([(v["name"], v["state"]) for v in body["vms"]], [("aa", "running")])
        page = vms_pages._list_page(Req(user))[2].decode()
        self.assertIn("aa", page)
        self.assertNotIn('data-vm="bb"', page)

    def test_new_vm_form_post(self):
        body = b"name=test9&template=ppro200&disk_gb=&cd=os.iso&group=lab&group=web&user=bob&autostart=1&csrf=tok"
        status_, headers, _ = vms_pages.h_new_post(Req(ADMIN, body), None)
        self.assertEqual((status_, headers["Location"]), (303, "/manage/"))
        d = vms.all_defs()["test9"]
        self.assertEqual((d["allowed_groups"], d["autostart"]), (["lab", "web"], True))
        status_, _, out = vms_pages.h_new_post(Req(ADMIN, b"name=BAD&template=ppro200"), None)
        self.assertEqual(status_, 400)
        self.assertIn(b"Name must be", out)

    def test_pages_escape_values(self):
        config.CFG["netgroups"]["<b>x"] = {"bridge": "br-x"}
        out = vms_pages._new_page(Req(ADMIN))[2].decode()
        self.assertNotIn("<b>x", out)
        self.assertIn("&lt;b&gt;x", out)

    def test_templates_listed(self):
        self.assertIn("ppro200", vms.list_templates())

    def cfg_of(self, name):
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.optionxform = str
        cfg.read(self.p("boxes", name, "86box.cfg"))
        return cfg

    def test_no_disk_and_no_cd_drive_by_default(self):
        self.make("plain")
        cfg = self.cfg_of("plain")
        self.assertEqual(cfg["Machine"]["machine"], "p65up5_cp6nd")
        for section in ("Hard disks", "Floppy and CD-ROM drives", "Video", "Input devices"):
            self.assertFalse(cfg.has_section(section), section)
        self.assertEqual(os.listdir(self.p("boxes/plain/disks")), [])

    def test_blank_vm_has_no_hardware_at_all(self):
        self.make("blank", template="")
        cfg = self.cfg_of("blank")
        self.assertEqual(cfg.sections(), ["General"])
        self.assertEqual(cfg["General"]["vid_resize"], "1")
        self.assertEqual(json.loads(rd(self.p("boxdefs/blank.json")))["template"], "")
        self.assertTrue(os.path.isdir(self.p("boxes/blank/disks")))

    def test_disk_is_only_made_when_asked_for(self):
        self.make("big", template="", disk_gb="1")
        self.assertEqual(self.cfg_of("big")["Hard disks"]["hdd_01_fn"], "disks/hdd1.img")
        self.assertTrue(os.path.exists(self.p("boxes/big/disks/hdd1.img")))
        self.make("zero", template="", disk_gb="0")
        self.assertFalse(os.path.exists(self.p("boxes/zero/disks/hdd1.img")))
        for bad in ("8", "-1", "abc"):
            with self.assertRaises(vms.VmError):
                self.make("bad", disk_gb=bad)

    def test_choosing_a_cd_adds_the_drive(self):
        self.make("withcd", template="", cd="os.iso")
        drives = self.cfg_of("withcd")["Floppy and CD-ROM drives"]
        self.assertEqual(drives["cdrom_01_parameters"], "1, atapi")
        self.assertEqual(drives["cdrom_01_image_path"], self.p("media/isos/os.iso"))
        self.make("later", template="")
        vms.edit_vm("later", [], False, [], cd="os.iso")
        self.assertEqual(self.cfg_of("later")["Floppy and CD-ROM drives"]["cdrom_01_parameters"], "1, atapi")

    def test_unknown_template_is_refused(self):
        with self.assertRaises(vms.VmError):
            self.make("nope", template="does-not-exist")

    def test_new_vm_form_offers_a_blank_template(self):
        out = vms_pages._new_page(Req(ADMIN))[2].decode()
        self.assertIn('<option value="">Blank', out)
        self.assertIn("network group is a private virtual switch", out)


if __name__ == "__main__":
    unittest.main()
