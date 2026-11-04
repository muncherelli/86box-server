import os
import re
import stat
import tempfile
import unittest
from unittest import mock

import cloudflare
import cloudflare_ui
import config
import edge
import secrets_store
import server

TOKEN = "cf-token-SECRET-9876"
ZONES = [{"id": "z1", "name": "example.com", "account_id": "acc", "account_name": "A"}]


class Req:
    def __init__(self, method="GET", path="/manage/cloudflare", body=b"", role="admin"):
        self.method, self.path, self.query = method, path, {}
        self.headers = {}
        self._body = body
        self.ip = "192.0.2.9"
        self.user = {"name": "boss", "role": role, "vms": []}
        self.session = {"csrf": "c"}

    def body(self):
        return self._body

    def form(self):
        import urllib.parse
        return {k: v[0] for k, v in urllib.parse.parse_qs(self._body.decode(), keep_blank_values=True).items()}


class FakeAuth:
    def __init__(self, role):
        self.role = role

    def authenticate(self, req):
        req.user = {"name": "u", "role": self.role, "vms": []}
        req.session = {"csrf": "c"}
        return req.user

    def csrf_ok(self, req):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "manager"))
        self.saved = dict(config.CFG)
        config.CFG.update(data_root=self.root)
        cloudflare_ui._zones_cache.update(at=0.0, last4="", zones=None, error="")
        edge.JOB.update(kind="", running=False, steps=[], done="", ok=None, again=False)

    def tearDown(self):
        config.CFG.clear()
        config.CFG.update(self.saved)
        self.tmp.cleanup()

    def audit(self):
        path = os.path.join(self.root, "manager", "audit.log")
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()


class SecretsTest(Base):
    def test_store_mode_info_and_audit(self):
        secrets_store.put("cf_api_token", TOKEN, "boss", "ip")
        path = os.path.join(self.root, "manager", "secrets.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(secrets_store.get("cf_api_token"), TOKEN)
        self.assertEqual(secrets_store.info("cf_api_token")["last4"], "9876")
        secrets_store.put("cf_api_token", TOKEN + "x", "boss", "ip")
        secrets_store.delete("cf_api_token", "boss", "ip")
        self.assertEqual(secrets_store.get("cf_api_token"), "")
        log = self.audit()
        self.assertIn("cf_api_token set", log)
        self.assertIn("cf_api_token replaced", log)
        self.assertIn("cf_api_token removed", log)
        self.assertNotIn(TOKEN, log)

    def test_rejects_bad_values(self):
        for bad in ("", "a b", "x\ny"):
            with self.assertRaises(ValueError):
                secrets_store.put("cf_api_token", bad)
        with self.assertRaises(ValueError):
            secrets_store.put("other", "v")


class PageTest(Base):
    def test_every_route_is_admin_only(self):
        self.assertTrue(cloudflare_ui.ROUTES)
        self.assertTrue(all(r[3] == "admin" for r in cloudflare_ui.ROUTES))
        server.load_routes()
        with mock.patch.object(server, "auth", FakeAuth("console")):
            for method, path in (("GET", "/manage/cloudflare"), ("POST", "/manage/cloudflare/token"),
                                 ("GET", "/manage/api/cloudflare/status")):
                self.assertEqual(server.dispatch(Req(method, path))[0], 403)

    def test_page_never_shows_the_token(self):
        secrets_store.put("cf_api_token", TOKEN)
        with mock.patch.object(cloudflare, "list_zones", return_value=ZONES), \
                mock.patch("units.is_active", return_value=False):
            status, _, body = cloudflare_ui.h_page(Req(), None)
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertNotIn(TOKEN, html)
        self.assertIn("9876", html)
        self.assertIn("1 zone(s)", html)
        self.assertIn('name="zones" value="example.com"', html)

    def test_save_token_verifies_first(self):
        with mock.patch.object(cloudflare, "list_zones", side_effect=cloudflare.CFError("GET /zones: Invalid token")):
            with mock.patch("units.is_active", return_value=False):
                status, _, body = cloudflare_ui.h_token(Req("POST", body=("token=" + TOKEN).encode()), None)
        self.assertEqual(status, 200)
        self.assertIn("Invalid token", body.decode())
        self.assertEqual(secrets_store.get("cf_api_token"), "")
        with mock.patch.object(cloudflare, "list_zones", return_value=ZONES):
            status, headers, _ = cloudflare_ui.h_token(Req("POST", body=("token=" + TOKEN).encode()), None)
        self.assertEqual(status, 303)
        self.assertEqual(secrets_store.get("cf_api_token"), TOKEN)

    def test_setup_starts_job_with_form_choices(self):
        secrets_store.put("cf_api_token", TOKEN)
        with mock.patch.object(cloudflare, "list_zones", return_value=ZONES), \
                mock.patch.object(edge, "start", return_value=True) as start:
            status, _, _ = cloudflare_ui.h_setup(
                Req("POST", body=b"zones=example.com&zone=example.com&name=box&staging=on&tunnel=on&email="), None)
        self.assertEqual(status, 303)
        kind, fn, state, token, replace = start.call_args[0]
        self.assertIs(fn, edge.sync)
        self.assertEqual(state["panel"], "box.example.com")
        self.assertEqual(state["zones"], {"example.com": {"id": "z1", "account_id": "acc"}})
        self.assertTrue(state["staging"] and state["tunnel"])
        self.assertEqual(token, TOKEN)
        self.assertFalse(replace)
        self.assertIn("box.example.com", self.audit())

    def test_setup_rejects_unknown_zone_and_bad_name(self):
        secrets_store.put("cf_api_token", TOKEN)
        with mock.patch.object(cloudflare, "list_zones", return_value=ZONES), \
                mock.patch.object(edge, "start") as start, mock.patch("units.is_active", return_value=False):
            for body in (b"zones=zz.net&name=box", b"zone=example.com&name=bad_name"):
                status, _, page = cloudflare_ui.h_setup(Req("POST", body=body), None)
                self.assertEqual(status, 200)
                self.assertTrue(re.search(r'class="error">[^<]+<', page.decode()))
        start.assert_not_called()

    def test_status_section_shows_routes_and_local_dns(self):
        edge.save({"zones": {"example.com": {"id": "z1", "account_id": "acc",
                                             "cert": {"expires": "2027-01-01T00:00:00Z"}}},
                   "panel": "box.example.com", "tunnel": True, "tunnel_id": "t1", "tunnel_name": "86server-x",
                   "dns": ["box.example.com"]})
        with mock.patch("units.is_active", return_value=True):
            html = cloudflare_ui._status_section(Req())
        self.assertIn("https://box.example.com/", html)
        self.assertIn("*.example.com</code>", html)
        self.assertIn("2027-01-01", html)
        self.assertIn("/manage/cloudflare/off", html)

    def test_status_json(self):
        status, headers, body = cloudflare_ui.h_status(Req(path="/manage/api/cloudflare/status"), None)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn(b'"running": false', body)


if __name__ == "__main__":
    unittest.main()
