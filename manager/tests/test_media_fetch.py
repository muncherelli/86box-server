import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import config
import media
import media_fetch
import selfupdate

PAYLOAD = b"86" * 50000  # 100 kB


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/octet-stream", extra=None, length=True):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if length:
            self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self.path
        if p == "/disk.iso":
            self.send(PAYLOAD)
        elif p == "/redir":
            self.redirect("/files/moved.iso")
        elif p == "/files/moved.iso":
            self.send(PAYLOAD)
        elif p == "/cd?token=secret":
            self.send(PAYLOAD, extra={"Content-Disposition": "attachment; filename*=UTF-8''My%20Disk%20%281%29.iso"})
        elif p == "/page":
            self.send(b"<html>nope</html>", ctype="text/html; charset=utf-8")
        elif p == "/to-metadata":
            self.redirect("http://169.254.169.254/latest/meta-data/")
        elif p == "/short.iso":
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD) * 2))
            self.end_headers()
            self.wfile.write(PAYLOAD)
        elif p == "/slow.iso":
            self.send_response(200)
            self.send_header("Content-Length", str(1024 * 1024 * 50))
            self.end_headers()
            try:
                for _ in range(500):
                    self.wfile.write(b"x" * 65536)
                    time.sleep(0.02)
            except OSError:
                pass
        elif p == "/404.iso":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send(PAYLOAD)


class AddressTest(unittest.TestCase):
    def test_refused_addresses(self):
        for a in ("127.0.0.1", "127.8.8.8", "::1", "::ffff:127.0.0.1", "169.254.169.254", "fe80::1", "0.0.0.0",
                  "::", "224.0.0.1", "240.0.0.1"):
            self.assertIsNotNone(media_fetch.address_problem(a), a)

    def test_own_address_refused(self):
        with mock.patch.object(media_fetch, "_is_local", side_effect=lambda ip: str(ip) == "192.0.2.5"):
            self.assertIn("own", media_fetch.address_problem("192.0.2.5"))
            self.assertIsNone(media_fetch.address_problem("192.0.2.6"))

    def test_lan_and_public_allowed(self):
        with mock.patch.object(media_fetch, "_is_local", return_value=False):
            for a in ("10.10.0.50", "192.168.1.20", "8.8.8.8", "2606:4700::1111"):
                self.assertIsNone(media_fetch.address_problem(a), a)

    def test_name_resolving_to_loopback_refused(self):
        with self.assertRaises(media_fetch.FetchError):
            media_fetch._resolve("localhost", 80)

    def test_check_url(self):
        for bad in ("", "ftp://x.org/a.iso", "file:///etc/passwd", "http://user:pw@x.org/a.iso", "http:///a.iso",
                    "https://x.org:99999/a.iso", "x" * 3000):
            with self.assertRaises(media_fetch.FetchError, msg=bad):
                media_fetch.check_url(bad)
        self.assertEqual(media_fetch.check_url("https://x.org/a.iso?b=1"), ("https", "x.org", 443, "/a.iso?b=1"))

    def test_redact_drops_query_and_user(self):
        self.assertEqual(media_fetch.redact("https://u:p@x.org/a.iso?sig=abc"), "https://x.org/a.iso")


class NameTest(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(media_fetch.clean_name("My Disk (1).iso"), "My_Disk_1_.iso")
        self.assertEqual(media_fetch.clean_name("../../etc/x.iso"), "x.iso")
        self.assertEqual(media_fetch.clean_name("..\\win\\a.img"), "a.img")
        self.assertEqual(media_fetch.clean_name("---.iso"), "iso")
        long = media_fetch.clean_name("a" * 300 + ".iso")
        self.assertTrue(long.endswith(".iso") and len(long) <= 121)
        self.assertIsNone(media.valid_name("iso", long))

    def test_pick_kind(self):
        self.assertEqual(media_fetch.pick_kind("x.iso", "auto"), "iso")
        self.assertEqual(media_fetch.pick_kind("x.iso", "floppy"), "iso")  # does not fit floppy, fits one folder
        self.assertEqual(media_fetch.pick_kind("x.img", "floppy"), "floppy")
        self.assertEqual(media_fetch.pick_kind("x.vhd", "auto"), "disk")
        with self.assertRaisesRegex(media_fetch.FetchError, "several folders"):
            media_fetch.pick_kind("x.img", "auto")
        with self.assertRaisesRegex(media_fetch.FetchError, "archive"):
            media_fetch.pick_kind("x.zip", "auto")
        with self.assertRaisesRegex(media_fetch.FetchError, "not a file type"):
            media_fetch.pick_kind("x.exe", "auto")


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        os.makedirs(os.path.join(root, "manager"))
        for k in media.KINDS:
            os.makedirs(os.path.join(root, "media", k))
        self.saved = dict(config.CFG)
        config.CFG.update(data_root=root, media_root=os.path.join(root, "media"))
        media_fetch._TEST_ALLOW.add("127.0.0.1")
        media_fetch.JOBS.clear()
        # /tmp is small; the free-space margin (1 GiB) is not what these tests are about.
        roomy = mock.patch("shutil.disk_usage", return_value=mock.Mock(free=100 * media.GIB))
        roomy.start()
        self.addCleanup(roomy.stop)

    def tearDown(self):
        for j in list(media_fetch.JOBS.values()):
            j["cancel"] = True
        deadline = time.time() + 5
        while media_fetch.active() and time.time() < deadline:
            time.sleep(0.05)
        media_fetch._TEST_ALLOW.discard("127.0.0.1")
        config.CFG.clear()
        config.CFG.update(self.saved)
        self.tmp.cleanup()

    def run_job(self, path, kind="auto", name="", timeout=10):
        job = media_fetch.start(self.base + path, kind, name, "boss", "192.0.2.9")
        deadline = time.time() + timeout
        while time.time() < deadline:
            j = media_fetch.JOBS[job["id"]]
            if j["finished"]:
                return j
            time.sleep(0.02)
        self.fail("job did not finish")

    def lib(self, kind, name):
        return os.path.join(config.CFG["media_root"], kind, name)

    def test_download(self):
        j = self.run_job("/disk.iso")
        self.assertEqual((j["state"], j["kind"], j["name"], j["bytes"]), ("done", "iso", "disk.iso", len(PAYLOAD)))
        with open(self.lib("iso", "disk.iso"), "rb") as f:
            self.assertEqual(f.read(), PAYLOAD)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.lib("iso", "x"))) if n.endswith(".part")], [])

    def test_redirect_name_comes_from_final_url(self):
        j = self.run_job("/redir")
        self.assertEqual((j["state"], j["name"]), ("done", "moved.iso"))

    def test_content_disposition_and_audit_has_no_query(self):
        j = self.run_job("/cd?token=secret")
        self.assertEqual((j["state"], j["name"]), ("done", "My_Disk_1_.iso"))
        with open(os.path.join(config.CFG["data_root"], "manager", "audit.log")) as f:
            log = f.read()
        self.assertIn("media.fetch", log)
        self.assertNotIn("secret", log)

    def test_given_name_and_folder(self):
        j = self.run_job("/disk.iso", kind="floppy", name="boot disk.img")
        self.assertEqual((j["state"], j["kind"], j["name"]), ("done", "floppy", "boot_disk.img"))

    def test_web_page_refused(self):
        j = self.run_job("/page.iso".replace(".iso", ""))
        self.assertEqual(j["state"], "failed")
        self.assertIn("web page", j["error"])

    def test_redirect_to_metadata_refused(self):
        j = self.run_job("/to-metadata")
        self.assertEqual(j["state"], "failed")
        self.assertIn("not allowed", j["error"])

    def test_http_error_and_short_body(self):
        self.assertIn("404", self.run_job("/404.iso")["error"])
        j = self.run_job("/short.iso")
        self.assertIn("ended early", j["error"])
        self.assertFalse(os.path.exists(self.lib("iso", "short.iso")))

    def test_existing_file_kept(self):
        with open(self.lib("iso", "disk.iso"), "wb") as f:
            f.write(b"mine")
        j = self.run_job("/disk.iso")
        self.assertIn("already exists", j["error"])
        with open(self.lib("iso", "disk.iso"), "rb") as f:
            self.assertEqual(f.read(), b"mine")

    def test_cancel_removes_part(self):
        job = media_fetch.start(self.base + "/slow.iso", "auto", "", "boss", "ip")
        deadline = time.time() + 5
        while media_fetch.JOBS[job["id"]]["bytes"] == 0 and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(media_fetch.active())
        media_fetch.JOBS[job["id"]]["cancel"] = True
        while not media_fetch.JOBS[job["id"]]["finished"] and time.time() < deadline + 5:
            time.sleep(0.02)
        self.assertEqual(media_fetch.JOBS[job["id"]]["state"], "cancelled")
        self.assertEqual(os.listdir(os.path.dirname(self.lib("iso", "x"))), [])

    def test_update_button_waits_for_downloads(self):
        state = os.path.join(self.tmp.name, "update")
        os.makedirs(state)
        dep = os.path.join(self.tmp.name, "deployed.json")
        with open(dep, "w") as f:
            json.dump({"commit": "a" * 40}, f)
        config.CFG.update(update_state_dir=state, deployed_file=dep)
        req = mock.Mock(user={"name": "boss"})
        with mock.patch.object(media_fetch, "active", return_value=True):
            status = selfupdate.h_update(req, None)[0]
        self.assertEqual(status, 409)
        self.assertEqual(selfupdate.h_update(req, None)[0], 200)


class CleanupTest(unittest.TestCase):
    def test_start_cleanup_removes_every_part(self):
        with tempfile.TemporaryDirectory() as root:
            saved = dict(config.CFG)
            config.CFG.update(media_root=os.path.join(root, "media"))
            try:
                os.makedirs(os.path.join(root, "media", "iso"))
                fresh = os.path.join(root, "media", "iso", ".a.iso.part")
                keep = os.path.join(root, "media", "iso", "a.iso")
                open(fresh, "w").close()
                open(keep, "w").close()
                media.cleanup_parts()            # normal: only old ones
                self.assertTrue(os.path.exists(fresh))
                media.cleanup_parts(max_age=0)   # manager start: all of them
                self.assertFalse(os.path.exists(fresh))
                self.assertTrue(os.path.exists(keep))
            finally:
                config.CFG.clear()
                config.CFG.update(saved)


if __name__ == "__main__":
    unittest.main()
