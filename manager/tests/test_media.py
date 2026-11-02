import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import config
import media
import store


def req(body=b"", length=None, headers=None, method="PUT", form=None):
    h = {"Content-Length": str(len(body) if length is None else length)} if length != "none" else {}
    h.update(headers or {})
    r = SimpleNamespace(rfile=io.BytesIO(body), length=len(body) if length in (None, "none") else length,
                        headers=h, user={"name": "admin", "role": "admin", "vms": []}, session={"csrf": "t"},
                        ip="1.2.3.4", method=method)
    r.form = lambda: form or {}
    return r


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        config.CFG["data_root"] = self.root
        for d in ("manager", "media/isos", "media/floppies", "media/disks", "vms"):
            os.makedirs(os.path.join(self.root, d))

        patcher = mock.patch("media.shutil.disk_usage", return_value=SimpleNamespace(free=10 * 1024 ** 3))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def m(self, kind, name):
        return SimpleNamespace(group=lambda i: (None, kind, name)[i])

    def put(self, kind, name, body=b"abc", **kw):
        return media.upload(req(body, **kw), self.m(kind, name))

    def test_upload_ok_mode_and_audit(self):
        self.assertEqual(self.put("isos", "a.iso")[0], 201)
        p = os.path.join(self.root, "media/isos/a.iso")
        self.assertEqual(open(p, "rb").read(), b"abc")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o664)
        self.assertEqual(os.listdir(os.path.join(self.root, "media/isos")), ["a.iso"])
        self.assertIn("media.upload", open(os.path.join(self.root, "manager/audit.log")).read())

    def test_bad_names_and_kind(self):
        for kind, name in [("isos", "../x.iso"), ("isos", "a.exe"), ("isos", ".hid.iso"), ("nope", "a.iso"),
                           ("isos", "a/b.iso"), ("isos", "a b.iso")]:
            self.assertEqual(self.put(kind, name)[0], 400, (kind, name))

    def test_url_encoded_dotdot(self):
        self.assertEqual(self.put("isos", "..%2Fx.iso")[0], 400)

    def test_no_content_length_411(self):
        self.assertEqual(self.put("isos", "a.iso", length="none")[0], 411)

    def test_exists_409_and_no_overwrite(self):
        self.put("isos", "a.iso", b"one")
        self.assertEqual(self.put("isos", "a.iso", b"two")[0], 409)
        self.assertEqual(open(os.path.join(self.root, "media/isos/a.iso"), "rb").read(), b"one")

    def test_short_body_removes_part(self):
        self.assertEqual(self.put("isos", "a.iso", b"abc", length=10)[0], 400)
        self.assertEqual(os.listdir(os.path.join(self.root, "media/isos")), [])

    def test_no_space_507(self):
        with mock.patch("media.shutil.disk_usage", return_value=SimpleNamespace(free=100)):
            self.assertEqual(self.put("isos", "a.iso")[0], 507)
        with mock.patch("media.shutil.disk_usage", return_value=SimpleNamespace(free=2 * 1024 ** 3)):
            self.assertEqual(self.put("isos", "b.iso")[0], 201)

    def test_listing_skips_junk(self):
        d = os.path.join(self.root, "media/isos")
        open(os.path.join(d, "ok.iso"), "w").write("x")
        open(os.path.join(d, ".hidden.iso"), "w").write("x")
        open(os.path.join(d, "bad name.iso"), "w").write("x")
        os.mkdir(os.path.join(d, "dir.iso"))
        os.symlink("/etc/hostname", os.path.join(d, "link.iso"))
        self.assertEqual([e["name"] for e in media.list_media("isos")], ["ok.iso"])

    def test_old_parts_cleaned(self):
        p = os.path.join(self.root, "media/isos/.old.iso.part")
        open(p, "w").write("x")
        os.utime(p, (1, 1))
        media.cleanup_parts()
        self.assertFalse(os.path.exists(p))

    def _vm(self, cdrom):
        d = os.path.join(self.root, "boxes/vm1")
        os.makedirs(d)
        open(os.path.join(d, "86box.cfg"), "w").write("[Floppy and CD-ROM drives]\ncdrom_01_image_path = %s\n" % cdrom)

    def test_delete_in_use_refused_relative_path_too(self):
        self.put("isos", "a.iso")
        self._vm("../../media/isos/a.iso")  # how 86Box saves it
        self.assertEqual(media.list_media("isos")[0]["used_by"], ["vm1"])
        r = media.delete(req(form={"confirm": "a.iso"}, method="POST"), self.m("isos", "a.iso"))
        self.assertEqual(r[0], 409)
        self.assertTrue(os.path.exists(os.path.join(self.root, "media/isos/a.iso")))

    def test_delete_needs_confirm_then_works(self):
        self.put("isos", "a.iso")
        self.assertEqual(media.delete(req(form={}, method="POST"), self.m("isos", "a.iso"))[0], 400)
        r = media.delete(req(form={"confirm": "a.iso"}, method="POST"), self.m("isos", "a.iso"))
        self.assertEqual(r[0], 303)
        self.assertEqual(media.list_media("isos"), [])

    def test_attach_iso_floppy_disk(self):
        self.put("isos", "a.iso")
        self.put("floppies", "f.img", b"floppy")
        self.put("disks", "d.vhd", b"disk")
        self._vm("")
        with mock.patch("units.is_active", return_value=False):
            self.assertIsNone(media.attach_file("vm1", "isos", "a.iso"))
            self.assertIsNone(media.attach_file("vm1", "floppies", "f.img"))
            self.assertIsNone(media.attach_file("vm1", "disks", "d.vhd"))
            self.assertIn("already", media.attach_file("vm1", "floppies", "f.img"))
        vm = os.path.join(self.root, "boxes/vm1")
        cfg = open(os.path.join(vm, "86box.cfg")).read()
        self.assertIn("cdrom_01_image_path = " + os.path.join(self.root, "media/isos/a.iso"), cfg)
        self.assertIn("fdd_01_fn = " + os.path.join(vm, "floppies/f.img"), cfg)
        self.assertEqual(open(os.path.join(vm, "disks/d.vhd"), "rb").read(), b"disk")
        self.assertEqual(os.stat(os.path.join(vm, "disks/d.vhd")).st_mode & 0o777, 0o660)

    def test_attach_refused_when_running(self):
        self.put("isos", "a.iso")
        self._vm("")
        with mock.patch("units.is_active", return_value=True):
            self.assertIn("Media menu", media.attach_file("vm1", "isos", "a.iso"))

    def test_attach_symlink_in_library_refused(self):
        os.symlink("/etc/hostname", os.path.join(self.root, "media/isos/l.iso"))
        self._vm("")
        with mock.patch("units.is_active", return_value=False):
            self.assertIn("regular", media.attach_file("vm1", "isos", "l.iso"))



class LocationsTest(unittest.TestCase):
    def test_page_shows_each_folder(self):
        with mock.patch.dict(media.config.CFG, {"data_root": "/srv/x", "share_group": "grp"}):
            html = media.locations_html()
        for kind in ("isos", "floppies", "disks"):
            self.assertIn("/srv/x/media/%s" % kind, html)
        self.assertIn("/srv/x/media<", html)
        self.assertIn("grp", html)
        self.assertIn(".iso", html)


if __name__ == "__main__":
    unittest.main()
