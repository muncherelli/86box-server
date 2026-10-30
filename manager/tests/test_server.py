import re
import unittest

import respond
import server


class FakeReq:
    def __init__(self, method="GET", path="/manage/health", origin=None):
        self.method, self.path, self.user, self.session = method, path, None, None
        self.headers = {"Origin": origin} if origin else {}
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


class DevModeTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        self.marker = os.path.join(tempfile.mkdtemp(), ".dev")
        self.old = respond.DEV_MARKER
        respond.DEV_MARKER = self.marker
        server.load_routes()

    def tearDown(self):
        respond.DEV_MARKER = self.old

    def test_dev_id_only_in_dev_mode(self):
        self.assertEqual(server.dispatch(FakeReq(path="/manage/dev-id"))[0], 404)
        open(self.marker, "w").close()
        status, _, body = server.dispatch(FakeReq(path="/manage/dev-id"))
        self.assertEqual((status, body), (200, server.BOOT_ID.encode()))

    def test_page_has_reload_meta_only_in_dev_mode(self):
        req = FakeReq()
        self.assertNotIn(b"dev-reload", respond.page(req, "t", "")[2])
        open(self.marker, "w").close()
        self.assertIn(b"dev-reload", respond.page(req, "t", "")[2])


if __name__ == "__main__":
    unittest.main()
