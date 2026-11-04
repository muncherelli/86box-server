import os
import re
import unittest

import respond
import server


class FakeReq:
    def __init__(self, method="GET", path="/manage/health", origin=None, proto=None, host=None):
        self.method, self.path, self.user, self.session = method, path, None, None
        self.headers = {"Origin": origin} if origin else {}
        if proto:
            self.headers["X-B86-Proto"] = proto
        if host:
            self.headers["Host"] = host
        self.query = {}


class ServerTest(unittest.TestCase):
    def setUp(self):
        server.load_routes()

    def test_health(self):
        self.assertEqual(server.dispatch(FakeReq())[0], 200)

    def test_unknown_and_wrong_method(self):
        self.assertEqual(server.dispatch(FakeReq(path="/nope"))[0], 404)
        self.assertEqual(server.dispatch(FakeReq(method="POST"))[0], 405)

    def test_static_list_is_fixed(self):
        self.assertEqual(server.dispatch(FakeReq(path="/manage/static/style.css"))[0], 200)
        self.assertEqual(server.dispatch(FakeReq(path="/manage/static/server.py"))[0], 404)

    def test_bad_origin(self):
        server.ROUTES.append(("POST", re.compile("/manage/x"), lambda r, m: respond.text("hi"), None))
        self.assertEqual(server.dispatch(FakeReq("POST", "/manage/x", "https://evil.example"))[0], 403)
        self.assertEqual(server.dispatch(FakeReq("POST", "/manage/x"))[0], 200)


class OriginTest(unittest.TestCase):
    def setUp(self):
        import config
        self.cfg = dict(config.CFG)
        config.CFG["manager_hosts"] = ["box.lan", "box.example.com"]
        server.load_routes()
        server.ROUTES.append(("POST", re.compile("/manage/x"), lambda r, m: respond.text("hi"), None))

    def tearDown(self):
        import config
        config.CFG.clear()
        config.CFG.update(self.cfg)

    def status(self, origin, proto, host=None):
        return server.dispatch(FakeReq("POST", "/manage/x", origin, proto, host))[0]

    def test_same_origin_on_any_name_or_address(self):
        # The panel is the server's default site: an unlisted name or IP works when Origin matches Host.
        self.assertEqual(self.status("https://192.168.1.50", "https", "192.168.1.50"), 200)
        self.assertEqual(self.status("http://other.lan", "http", "other.lan"), 200)
        self.assertEqual(self.status("https://Other.LAN", "https", "other.lan:443"), 200)
        self.assertEqual(self.status("http://[fd00::5]", "http", "[fd00::5]:80"), 200)

    def test_each_host_on_its_own_scheme(self):
        self.assertEqual(self.status("http://box.lan", "http"), 200)
        self.assertEqual(self.status("https://box.lan", "https"), 200)
        self.assertEqual(self.status("https://box.example.com", "https"), 200)

    def test_refused(self):
        self.assertEqual(self.status("http://box.lan", "https"), 403)   # scheme mismatch
        self.assertEqual(self.status("https://box.lan", None), 403)     # no proto header = http
        self.assertEqual(self.status("https://evil.example", "https"), 403)
        self.assertEqual(self.status("https://box.lan:8443", "https"), 403)
        self.assertEqual(self.status("null", "https"), 403)
        self.assertEqual(self.status("https://evil.example", "https", "192.168.1.50"), 403)  # cross-site
        self.assertEqual(self.status("http://192.168.1.50", "https", "192.168.1.50"), 403)     # scheme mismatch
        self.assertEqual(self.status("null", "https", "null"), 403)


class ThemeTest(unittest.TestCase):
    def test_page_has_the_theme_switcher_and_early_script(self):
        page = respond.page(FakeReq(), "t", "")[2].decode()
        self.assertIn('id="theme-switch"', page)
        for value in ("system", "light", "dark"):
            self.assertIn('<option value="%s">' % value, page)
        self.assertLess(page.index("b86-theme"), page.index("style.css"))  # applied before the css loads

    def test_stylesheet_follows_the_system_and_has_both_themes(self):
        css = open(os.path.join(os.path.dirname(server.__file__), "static", "style.css")).read()
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertNotIn("#fff;", css.split("body {")[1])  # no hard-coded page colors left after the variables


if __name__ == "__main__":
    unittest.main()
