import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

import config
import sites
import store

HERE = os.path.dirname(os.path.abspath(__file__))
SNIPPETS = "(b86_edge) {\n}\n(b86_auth) {\n}\n"
GOLDEN_SITE = {"host": "legacy.example.com", "vm": "nt4-web", "guest_ip": "10.86.1.10", "port": 80}


def site(**kw):
    return dict(GOLDEN_SITE, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        for d in ("vmdefs", "manager", "sites.d"):
            os.makedirs(os.path.join(root, d))
        self.sites_dir = os.path.join(root, "sites.d")
        snip = os.path.join(root, "snippets.caddy")
        with open(snip, "w") as f:
            f.write(SNIPPETS)
        self.saved = dict(config.CFG)
        config.CFG.update(data_root=root, sites_dir=self.sites_dir, validate_snippets=snip, edge_mode="acme",
                          publish_domains=["example.com"],
                          netgroups={"web": {"bridge": "br-web", "cidr": "10.86.1.0/24"}})
        store.write_json(os.path.join(root, "vmdefs", "nt4-web.json"), {"allowed_groups": ["web"]})

    def tearDown(self):
        config.CFG.clear()
        config.CFG.update(self.saved)
        self.tmp.cleanup()


class RenderTest(Base):
    def test_golden(self):
        with open(os.path.join(HERE, "golden", "default.caddy")) as f:
            self.assertEqual(sites.render_site(site()), f.read())

    def test_bad_values(self):
        bad = [
            site(host="legacy.example.com\nrespond 200"),
            site(host="legacy.other.org"),
            site(host="LEGACY.example.com"),
            site(guest_ip="127.0.0.1"),
            site(guest_ip="10.86.1.1"),
            site(guest_ip="10.86.1.0"),
            site(guest_ip="10.86.1.255"),
            site(guest_ip="10.86.1.10\n"),
            site(port=0),
            site(port=70000),
            site(port="80"),
            site(vm="nope"),
            site(vm="../x"),
            site(host_header="a b"),
            site(rules={"methods": ["TRACE"]}),
            site(rules={"methods": []}),
            site(rules={"allow_paths": ["/a/../"]}),
            site(rules={"allow_paths": ["/a\nb/"]}),
            site(rules={"allow_paths": ["a/"]}),
            site(rules={"allow_paths": ["/public/"], "strip_prefix": "/other/"}),
            site(rules={"strip_prefix": "/"}),
            site(rules={"max_body": "5GB"}),
        ]
        for s in bad:
            with self.assertRaises(ValueError, msg=repr(s)):
                sites.render_site(s)

    def test_variants(self):
        text = sites.render_site(site(rules={
            "methods": ["POST", "GET"], "allow_paths": ["/public/", "/a.b/"], "strip_prefix": "/public/",
            "block_encoded": False, "iis_profile": {"asa_ext": True}, "max_body": "10MB"}, host_header="legacy"))
        self.assertIn("max_size 10MB", text)
        self.assertNotIn("@b86enc", text)
        self.assertIn("@b86method not method GET POST\n", text)
        self.assertIn("@b86iis path_regexp (?i)\\.(asa|cdx|cer)$\n", text)
        self.assertIn("@b86deny not path_regexp ^(/public/|/a\\.b/)\n", text)
        self.assertIn("\t\turi strip_prefix /public\n", text)
        self.assertIn("\t\treverse_proxy 10.86.1.10:80 {\n\t\t\theader_up Host legacy\n\t\t}\n", text)

    def test_no_iis_items(self):
        text = sites.render_site(site(rules={"iis_profile": {}}))
        self.assertNotIn("b86iis", text)

    def test_form(self):
        data = {"host": ["legacy.example.com"], "vm": ["nt4-web"], "guest_ip": ["10.86.1.10"], "port": ["80"],
                "methods": ["GET", "POST"], "allow_paths": ["/\n/x/\n"], "enabled": ["on"], "iis_scripts": ["on"],
                "iis_admin_dirs": ["on"]}
        s = sites.site_from_form(data)
        self.assertEqual(s["rules"]["methods"], ["GET", "POST"])
        self.assertEqual(s["rules"]["allow_paths"], ["/", "/x/"])
        self.assertTrue(s["rules"]["iis_profile"]["admin_dirs"])
        self.assertFalse(s["rules"]["iis_profile"]["asa_ext"])
        self.assertFalse(s["rules"]["block_encoded"])
        with self.assertRaises(ValueError):
            sites.site_from_form(dict(data, port=["x"]))


@unittest.skipUnless(shutil.which("caddy"), "caddy not installed")
class ApplyTest(Base):
    def files(self):
        return sorted(os.listdir(self.sites_dir))

    def read(self, name):
        with open(os.path.join(self.sites_dir, name)) as f:
            return f.read()

    def test_apply_writes_and_reloads(self):
        with mock.patch("units.run", return_value=(True, "")) as run:
            self.assertEqual(sites.apply_sites([site()]), (True, "applied"))
        self.assertEqual(self.files(), ["legacy.example.com.caddy"])
        path = os.path.join(self.sites_dir, "legacy.example.com.caddy")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        run.assert_called_once_with("reload", "caddy.service")

    def test_disabled_site_removed(self):
        with mock.patch("units.run", return_value=(True, "")):
            sites.apply_sites([site()])
            sites.apply_sites([site(enabled=False)])
        self.assertEqual(self.files(), [])

    def test_caddy_failure_leaves_files(self):
        with mock.patch("units.run", return_value=(True, "")):
            sites.apply_sites([site()])
        fake = os.path.join(self.tmp.name, "caddy")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho 'bad config' >&2\nexit 1\n")
        os.chmod(fake, 0o755)
        with mock.patch.dict(os.environ, {"B86_CADDY": fake}), mock.patch("units.run") as run:
            ok, msg = sites.apply_sites([])
        self.assertFalse(ok)
        self.assertIn("bad config", msg)
        self.assertEqual(self.files(), ["legacy.example.com.caddy"])
        run.assert_not_called()

    def test_render_error_changes_nothing(self):
        with mock.patch("units.run") as run:
            ok, _ = sites.apply_sites([site(guest_ip="127.0.0.1")])
        self.assertFalse(ok)
        self.assertEqual(self.files(), [])
        run.assert_not_called()

    def test_reload_failure_rolls_back(self):
        with mock.patch("units.run", return_value=(True, "")):
            sites.apply_sites([site()])
        before = self.read("legacy.example.com.caddy")
        results = [(False, "boom"), (True, "")]
        with mock.patch("units.run", side_effect=results) as run:
            ok, msg = sites.apply_sites([site(rules={"max_body": "10MB"}), site(host="two.example.com")])
        self.assertFalse(ok)
        self.assertIn("boom", msg)
        self.assertEqual(self.files(), ["legacy.example.com.caddy"])
        self.assertEqual(self.read("legacy.example.com.caddy"), before)
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
