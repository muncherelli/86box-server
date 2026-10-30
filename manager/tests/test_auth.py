import json
import os
import re
import tempfile
import unittest

import auth
import config
import server
import store


class FakeReq:
    def __init__(self, method="GET", path="/manage/", form=None, cookie=None, ip="1.2.3.4", headers=None, query=None):
        self.method, self.path, self.ip = method, path, ip
        self.query = query or {}
        self._form = form or {}
        self.headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            self.headers["Cookie"] = "b86s=" + cookie
        self.headers.update(headers or {})
        self.user = self.session = None
        self.length = 0

    def form(self):
        return self._form


def cookie_of(headers):
    return re.match(r"b86s=([^;]+);", headers["Set-Cookie"]).group(1)


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        config.CFG["data_root"] = self.dir.name
        config.CFG["manager_host"] = "mgr.example.com"
        os.makedirs(os.path.join(self.dir.name, "manager"))
        auth.SESSIONS.clear()
        auth.FAILS.clear()
        auth.create_user("admin", "correct horse battery", "admin")
        auth.create_user("bob", "another long password", "console", ["vm-a"])
        server.load_routes()

    def tearDown(self):
        self.dir.cleanup()

    def test_hash_and_verify(self):
        h = auth.hash_password("a long enough password")
        self.assertTrue(h.startswith("scrypt$16384$8$1$"))
        self.assertTrue(auth.verify_password("a long enough password", h))
        self.assertFalse(auth.verify_password("wrong", h))
        self.assertFalse(auth.verify_password("x", "garbage"))

    def test_users_file_mode_and_password_rules(self):
        self.assertEqual(os.stat(auth._users_path()).st_mode & 0o777, 0o600)
        with self.assertRaises(auth.AuthError):
            auth.create_user("shorty", "short", "console")
        with self.assertRaises(auth.AuthError):
            auth.create_user("Bad Name", "long enough password", "console")

    def test_wrong_and_unknown_user(self):
        self.assertEqual(auth.login("9.9.9.9", "admin", "nope nope nope")[0], "bad")
        self.assertEqual(auth.login("9.9.9.9", "ghost", "nope nope nope")[0], "bad")
        self.assertEqual(auth.login("9.9.9.9", "admin", "correct horse battery")[0], "ok")

    def test_session_expiry(self):
        t = auth.create_session("admin", now=1000)
        self.assertIsNotNone(auth.lookup_session(t, now=1000 + 11 * 3600))      # idle 11 h: ok, and refreshed
        self.assertIsNotNone(auth.lookup_session(t, now=1000 + 22 * 3600))
        self.assertIsNone(auth.lookup_session(t, now=1000 + 35 * 3600))         # idle 13 h
        t2 = auth.create_session("admin", now=0)
        for h in range(0, 8 * 24, 10):                                          # keep it busy
            s = auth.lookup_session(t2, now=h * 3600)
            if s is None:
                break
        self.assertLess(h, 8 * 24)                                              # absolute 7 days cut it off
        self.assertIsNone(auth.lookup_session(t2, now=7 * 24 * 3600 + 1))

    def test_rate_limit_blocks_sixth_even_with_right_password(self):
        for i in range(5):
            self.assertEqual(auth.login("5.5.5.5", "admin", "wrong wrong wrong", now=100 + i)[0], "bad")
        self.assertEqual(auth.login("5.5.5.5", "admin", "correct horse battery", now=110)[0], "blocked")
        self.assertEqual(auth.login("6.6.6.6", "admin", "correct horse battery", now=110)[0], "ok")  # other IP
        self.assertEqual(auth.login("5.5.5.5", "admin", "correct horse battery", now=100 + 901)[0], "ok")

    def login_via_handler(self):
        status, headers, _ = server.dispatch(FakeReq("POST", "/manage/login",
                                                     {"username": "admin", "password": "correct horse battery", "next": "/manage/users"}))
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/manage/users")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return cookie_of(headers)

    def test_login_flow_and_csrf(self):
        tok = self.login_via_handler()
        self.assertEqual(server.dispatch(FakeReq(path="/manage/users", cookie=tok))[0], 200)
        csrf = auth.SESSIONS[tok]["csrf"]
        no_csrf = server.dispatch(FakeReq("POST", "/manage/logout", cookie=tok))
        self.assertEqual(no_csrf[0], 403)
        bad = server.dispatch(FakeReq("POST", "/manage/logout", {"csrf": "x"}, cookie=tok))
        self.assertEqual(bad[0], 403)
        self.assertEqual(server.dispatch(FakeReq("POST", "/manage/logout", headers={"X-CSRF": csrf}, cookie=tok))[0], 303)
        self.assertEqual(server.dispatch(FakeReq(path="/manage/users", cookie=tok))[0], 303)

    def test_not_logged_in(self):
        status, headers, _ = server.dispatch(FakeReq(path="/manage/users"))
        self.assertEqual((status, headers["Location"].split("?")[0]), (303, "/manage/login"))
        self.assertEqual(server.dispatch(FakeReq(path="/manage/api/vms"))[0] in (401, 404), True)

    def test_console_user_cannot_open_users_page(self):
        status, headers, _ = server.dispatch(FakeReq("POST", "/manage/login", {"username": "bob", "password": "another long password"}))
        tok = cookie_of(headers)
        self.assertEqual(server.dispatch(FakeReq(path="/manage/users", cookie=tok))[0], 403)
        self.assertEqual(server.dispatch(FakeReq(path="/manage/account", cookie=tok))[0], 200)

    def test_login_next_is_checked(self):
        for bad in ("//evil.example", "https://evil.example", "/other"):
            status, headers, _ = server.dispatch(FakeReq("POST", "/manage/login",
                                                         {"username": "admin", "password": "correct horse battery", "next": bad}))
            self.assertEqual(headers["Location"], "/manage/")

    def test_last_admin_protection(self):
        with self.assertRaises(auth.AuthError):
            auth.delete_user("admin")
        with self.assertRaises(auth.AuthError):
            auth.set_role_vms("admin", "console", [])
        auth.create_user("admin2", "second admin password", "admin")
        auth.delete_user("admin")

    def test_changes_end_sessions(self):
        t = auth.create_session("bob")
        auth.set_password("bob", "brand new password")
        self.assertIsNone(auth.lookup_session(t))
        t = auth.create_session("bob")
        auth.set_role_vms("bob", "console", ["vm-b"])
        self.assertIsNone(auth.lookup_session(t))
        t = auth.create_session("bob")
        auth.delete_user("bob")
        self.assertIsNone(auth.lookup_session(t))

    def test_vm_helpers(self):
        self.assertEqual(auth.console_users(), {"bob": {"vms": ["vm-a"]}})
        auth.set_vm_users("vm-b", ["bob", "admin"])   # admin is not a console user: ignored
        self.assertEqual(auth.console_users()["bob"]["vms"], ["vm-a", "vm-b"])
        auth.set_vm_users("vm-a", [])
        auth.remove_vm("vm-b")
        self.assertEqual(auth.console_users()["bob"]["vms"], [])
        self.assertTrue(auth.can_use_vm({"role": "admin", "vms": []}, "x"))
        self.assertFalse(auth.can_use_vm({"role": "console", "vms": ["a"]}, "b"))

    def test_seed(self):
        os.unlink(auth._users_path())
        seed = os.path.join(self.dir.name, "manager", "seed.json")
        store.write_json(seed, {"admin_user": "root1", "admin_password": "seeded password 123"}, mode=0o600)
        auth.seed_if_needed()
        self.assertFalse(os.path.exists(seed))
        self.assertEqual(auth.login("1.1.1.1", "root1", "seeded password 123")[0], "ok")
        store.write_json(seed, {"admin_user": "other", "admin_password": "another seeded password"}, mode=0o600)
        auth.seed_if_needed()   # users.json exists: seed is left alone
        self.assertTrue(os.path.exists(seed))

    def test_audit_has_no_secrets(self):
        auth.login("1.1.1.1", "admin", "SECRET-WRONG-PASSWORD")
        server.dispatch(FakeReq("POST", "/manage/login", {"username": "admin", "password": "SECRET-WRONG-PASSWORD"}))
        with open(os.path.join(self.dir.name, "manager", "audit.log")) as f:
            text = f.read()
        self.assertIn("login-fail", text)
        self.assertNotIn("SECRET-WRONG-PASSWORD", text)


    def test_forward_auth_check(self):
        def check(user, pw, token):
            tok = auth.login("1.2.3.4", user, pw)[1] if user else None
            h = {"X-Forwarded-Uri": "/websockify?token=" + token}
            return auth.auth_check(FakeReq(cookie=tok, headers=h), None)[0]
        self.assertEqual(check(None, None, "vm-a"), 401)
        self.assertEqual(check("bob", "another long password", "vm-a"), 200)
        self.assertEqual(check("bob", "another long password", "vm-b"), 403)
        self.assertEqual(check("admin", "correct horse battery", "vm-b"), 200)
        self.assertEqual(check("admin", "correct horse battery", "Bad_Name"), 403)
        bad = FakeReq(headers={"X-Forwarded-Uri": "/other?token=vm-a"})
        self.assertEqual(auth.auth_check(bad, None)[0], 403)


if __name__ == "__main__":
    unittest.main()
