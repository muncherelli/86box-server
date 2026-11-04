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
        config.CFG["media_root"] = os.path.join(self.root, "media")
        for d in ("manager", "media/iso", "media/floppy", "media/disk", "vms"):
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
        self.assertEqual(self.put("iso", "a.iso")[0], 201)
        p = os.path.join(self.root, "media/iso/a.iso")
        self.assertEqual(open(p, "rb").read(), b"abc")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o664)
        self.assertEqual(os.listdir(os.path.join(self.root, "media/iso")), ["a.iso"])
        self.assertIn("media.upload", open(os.path.join(self.root, "manager/audit.log")).read())

    def test_bad_names_and_kind(self):
        for kind, name in [("iso", "../x.iso"), ("iso", "a.exe"), ("iso", ".hid.iso"), ("nope", "a.iso"),
                           ("iso", "a/b.iso"), ("iso", "a b.iso")]:
            self.assertEqual(self.put(kind, name)[0], 400, (kind, name))

    def test_url_encoded_dotdot(self):
        self.assertEqual(self.put("iso", "..%2Fx.iso")[0], 400)

    def test_no_content_length_411(self):
        self.assertEqual(self.put("iso", "a.iso", length="none")[0], 411)

    def test_exists_409_and_no_overwrite(self):
        self.put("iso", "a.iso", b"one")
        self.assertEqual(self.put("iso", "a.iso", b"two")[0], 409)
        self.assertEqual(open(os.path.join(self.root, "media/iso/a.iso"), "rb").read(), b"one")

    def test_short_body_removes_part(self):
        self.assertEqual(self.put("iso", "a.iso", b"abc", length=10)[0], 400)
        self.assertEqual(os.listdir(os.path.join(self.root, "media/iso")), [])

    def test_no_space_507(self):
        with mock.patch("media.shutil.disk_usage", return_value=SimpleNamespace(free=100)):
            self.assertEqual(self.put("iso", "a.iso")[0], 507)
        with mock.patch("media.shutil.disk_usage", return_value=SimpleNamespace(free=2 * 1024 ** 3)):
            self.assertEqual(self.put("iso", "b.iso")[0], 201)

    def test_listing_skips_junk(self):
        d = os.path.join(self.root, "media/iso")
        open(os.path.join(d, "ok.iso"), "w").write("x")
        open(os.path.join(d, ".hidden.iso"), "w").write("x")
        open(os.path.join(d, "bad name.iso"), "w").write("x")
        os.mkdir(os.path.join(d, "dir.iso"))
        os.symlink("/etc/hostname", os.path.join(d, "link.iso"))
        self.assertEqual([e["name"] for e in media.list_media("iso")], ["ok.iso"])

    def test_old_parts_cleaned(self):
        p = os.path.join(self.root, "media/iso/.old.iso.part")
        open(p, "w").write("x")
        os.utime(p, (1, 1))
        media.cleanup_parts()
        self.assertFalse(os.path.exists(p))

    def _vm(self, cdrom):
        d = os.path.join(self.root, "boxes/vm1")
        os.makedirs(d)
        open(os.path.join(d, "86box.cfg"), "w").write("[Floppy and CD-ROM drives]\ncdrom_01_image_path = %s\n" % cdrom)

    def test_delete_in_use_refused_relative_path_too(self):
        self.put("iso", "a.iso")
        self._vm("../../media/iso/a.iso")  # how 86Box saves it
        self.assertEqual(media.list_media("iso")[0]["used_by"], ["vm1"])
        r = media.delete(req(form={"confirm": "a.iso"}, method="POST"), self.m("iso", "a.iso"))
        self.assertEqual(r[0], 409)
        self.assertTrue(os.path.exists(os.path.join(self.root, "media/iso/a.iso")))

    def test_delete_needs_confirm_then_works(self):
        self.put("iso", "a.iso")
        self.assertEqual(media.delete(req(form={}, method="POST"), self.m("iso", "a.iso"))[0], 400)
        r = media.delete(req(form={"confirm": "a.iso"}, method="POST"), self.m("iso", "a.iso"))
        self.assertEqual(r[0], 303)
        self.assertEqual(media.list_media("iso"), [])

    def page(self, **query):
        r = req(method="GET")
        r.query = {k: [str(v)] for k, v in query.items()}
        with mock.patch("config.tunnel_on", return_value=False):
            return media.media_page(r, None)[2].decode()

    def files(self, kind, names):
        for i, n in enumerate(names):
            p = os.path.join(self.root, "media", kind, n)
            open(p, "w").write("x" * (i + 1))
            os.utime(p, (1000 + i, 1000 + i))

    def test_page_tabs_tags_and_folder(self):
        self.files("iso", ["a.iso", "b.iso"])
        self.files("floppy", ["f.img"])
        out = self.page()
        self.assertIn('aria-current="page">CD/DVD <span class="count">2</span>', out)
        self.assertIn('>Floppy <span class="count">1</span>', out)
        self.assertIn('<span class="tag">.iso</span>', out)
        self.assertNotIn('<span class="tag">.vfd</span>', out)       # only the open tab's types
        self.assertNotIn("<th>File types</th>", out)
        self.assertIn("<code>%s</code>" % os.path.join(self.root, "media/iso"), out)
        self.assertIn('<th class="col-used">Used by</th>', out)
        out = self.page(kind="floppy")
        self.assertIn('<span class="tag">.vfd</span>', out)
        self.assertIn("f.img", out)
        self.assertNotIn("a.iso", out)
        self.assertNotIn('<th class="col-used">Used by</th>', out)
        self.assertIn('<option value="floppy" selected>', out)       # upload goes to the open tab's kind
        self.assertIn("CD/DVD images", self.page(kind="../etc"))       # unknown kind falls back

    def test_page_search_sort_and_paging(self):
        names = ["disk%03d.iso" % i for i in range(120)]
        self.files("iso", names)
        out = self.page()
        self.assertIn("1–50 of 120", out)
        self.assertIn("disk000.iso", out)
        self.assertNotIn("disk050.iso", out)
        out = self.page(page=3)
        self.assertIn("101–120 of 120", out)
        self.assertIn("disk119.iso", out)
        self.assertIn("Page 3 of 3", self.page(page=99))                # clamped
        self.assertIn("1–100 of 120", self.page(per=100))
        self.assertIn("1–50 of 120", self.page(per=7))                  # only the offered sizes
        out = self.page(sort="size")                                    # largest first
        self.assertLess(out.index("disk119.iso"), out.index("disk118.iso"))
        out = self.page(sort="modified", dir="asc")
        self.assertLess(out.index("disk000.iso"), out.index("disk001.iso"))
        out = self.page(q="DISK11")
        self.assertIn("1–10 of 10", out)
        self.assertNotIn("disk010.iso", out)
        self.assertIn("No files match <b>nope</b>.", self.page(q="nope"))
        self.assertIn("&lt;x&gt;", self.page(q="<x>"))                  # escaped

    def post(self, kind, body):
        r = req(body, method="POST")
        r.body = lambda: body
        return media.delete_many(r, SimpleNamespace(group=lambda i: (None, kind)[i]))

    def test_delete_many(self):
        self.files("iso", ["a.iso", "b.iso", "c.iso"])
        self._vm("../../media/iso/c.iso")
        listed = lambda: [e["name"] for e in media.list_media("iso")]
        self.assertEqual(self.post("iso", b"name=a.iso&name=b.iso&confirm=1")[0], 400)   # count must match
        self.assertEqual(listed(), ["a.iso", "b.iso", "c.iso"])
        self.assertEqual(self.post("iso", b"confirm=0")[0], 400)
        r = self.post("iso", b"name=a.iso&name=c.iso&confirm=2&back=%2Fmanage%2Fmedia%3Fpage%3D2")
        self.assertEqual(r[0], 409)                                     # c.iso is in use
        self.assertIn("Deleted 1 of 2", r[2].decode())
        self.assertIn("c.iso is in use by: vm1", r[2].decode())
        self.assertEqual(listed(), ["b.iso", "c.iso"])
        r = self.post("iso", b"name=b.iso&confirm=1&back=%2Fmanage%2Fmedia%3Fpage%3D2")
        self.assertEqual((r[0], r[1]["Location"]), (303, "/manage/media?page=2"))
        r = self.post("iso", b"name=c.iso&name=..%2Fx.iso&confirm=2&back=https%3A%2F%2Fevil")
        self.assertIn('href="/manage/media"', r[2].decode())            # back only to the media page
        self.assertEqual(self.post("nope", b"name=a.iso&confirm=1")[0], 400)

    def test_attach_iso_floppy_disk(self):
        self.put("iso", "a.iso")
        self.put("floppy", "f.img", b"floppy")
        self.put("disk", "d.vhd", b"disk")
        self._vm("")
        with mock.patch("units.is_active", return_value=False):
            self.assertIsNone(media.attach_file("vm1", "iso", "a.iso"))
            self.assertIsNone(media.attach_file("vm1", "floppy", "f.img"))
            self.assertIsNone(media.attach_file("vm1", "disk", "d.vhd"))
            self.assertIn("already", media.attach_file("vm1", "floppy", "f.img"))
        vm = os.path.join(self.root, "boxes/vm1")
        cfg = open(os.path.join(vm, "86box.cfg")).read()
        self.assertIn("cdrom_01_image_path = " + os.path.join(self.root, "media/iso/a.iso"), cfg)
        self.assertIn("fdd_01_fn = " + os.path.join(vm, "floppies/f.img"), cfg)
        self.assertEqual(open(os.path.join(vm, "disks/d.vhd"), "rb").read(), b"disk")
        self.assertEqual(os.stat(os.path.join(vm, "disks/d.vhd")).st_mode & 0o777, 0o660)

    def test_attach_refused_when_running(self):
        self.put("iso", "a.iso")
        self._vm("")
        with mock.patch("units.is_active", return_value=True):
            self.assertIn("Media menu", media.attach_file("vm1", "iso", "a.iso"))

    def test_attach_symlink_in_library_refused(self):
        os.symlink("/etc/hostname", os.path.join(self.root, "media/iso/l.iso"))
        self._vm("")
        with mock.patch("units.is_active", return_value=False):
            self.assertIn("regular", media.attach_file("vm1", "iso", "l.iso"))



class LocationsTest(unittest.TestCase):
    def test_page_shows_the_library_folder(self):
        with mock.patch.dict(media.config.CFG, {"media_root": "/srv/x/media", "share_group": "grp"}):
            html = media.locations_html()
        self.assertIn("/srv/x/media<", html)
        self.assertIn("grp", html)

    def test_extensions_follow_86box(self):
        ok = [("iso", "a.ISO"), ("iso", "a.cue"), ("floppy", "a.vfd"), ("floppy", "a.86f"), ("floppy", "a.ima"),
              ("disk", "a.hdi"), ("disk", "a.vhd"), ("zip", "a.zdi"), ("mo", "a.mdi"), ("tape", "a.tap"),
              ("cassette", "a.cas"), ("cartridge", "a.jrc")]
        for kind, name in ok:
            self.assertIsNone(media.valid_name(kind, name), (kind, name))
        for kind, name in [("iso", "a.exe"), ("disk", "a.iso"), ("tape", "a"), ("zip", "a.vhd")]:
            self.assertIsNotNone(media.valid_name(kind, name), (kind, name))

    def test_only_iso_floppy_disk_attach_from_the_page(self):
        self.assertIn("console", media.attach_file("vm1", "zip", "a.zdi"))


if __name__ == "__main__":
    unittest.main()
