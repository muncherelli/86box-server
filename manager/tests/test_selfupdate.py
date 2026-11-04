import datetime
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import config
import respond
import selfupdate
import server

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK_SCRIPT = os.path.join(HERE, "..", "..", "ansible", "roles", "selfupdate", "files", "86server-update-check")


class Req:
    def __init__(self, method="GET", path="/manage/api/update", role="admin"):
        self.method, self.path, self.query = method, path, {}
        self.headers = {}
        self.user = {"name": "boss", "role": role, "vms": []} if role else None
        self.session = {"csrf": "c"} if role else None


class FakeAuth:
    def __init__(self, role):
        self.role = role

    def authenticate(self, req):
        req.user = {"name": "u", "role": self.role, "vms": []}
        req.session = {"csrf": "c"}
        return req.user

    def csrf_ok(self, req):
        return True


def now_iso(delta=0):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta)
    return t.isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "manager"))
        self.state = os.path.join(self.root, "update")
        os.makedirs(self.state)
        self.saved = dict(config.CFG)
        config.CFG.update(data_root=self.root, update_state_dir=self.state,
                          deployed_file=os.path.join(self.root, "deployed.json"))

    def tearDown(self):
        config.CFG.clear()
        config.CFG.update(self.saved)
        self.tmp.cleanup()

    def put(self, path, obj):
        with open(path, "w") as f:
            json.dump(obj, f)

    def deploy(self, commit="a" * 40, **kw):
        self.put(config.CFG["deployed_file"], dict(commit=commit, short=commit[:7], subject="old", branch="trunk",
                                                   dirty=False, at="2026-11-04T20:00:00+00:00", **kw))

    def check(self, **kw):
        self.put(os.path.join(self.state, "check.json"), kw)


class StatusTest(Base):
    def test_nothing_deployed_means_disabled(self):
        st = selfupdate.status()
        self.assertFalse(st["enabled"])
        self.assertFalse(st["available"])
        self.assertEqual(selfupdate.badge(Req()), "")

    def test_available_with_commits(self):
        self.deploy()
        self.check(deployed="a" * 40, head="b" * 40, available=True, count=2,
                   commits=[{"short": "bbbbbbb", "subject": "feat: <new>"}, {"short": "ccccccc", "subject": "fix"}])
        st = selfupdate.status()
        self.assertTrue(st["enabled"] and st["available"])
        self.assertEqual(st["count"], 2)
        html = selfupdate.badge(Req())
        self.assertIn("Update available · 2 new commits", html)
        self.assertIn("feat: &lt;new&gt;", html)  # escaped
        self.assertNotIn("<new>", html)

    def test_check_against_an_older_deploy_is_ignored(self):
        self.deploy(commit="b" * 40)  # just deployed what the stale check called new
        self.check(deployed="a" * 40, head="b" * 40, available=True, count=1, commits=[])
        self.assertFalse(selfupdate.status()["available"])
        self.assertEqual(selfupdate.badge(Req()), "")

    def test_check_error_is_not_available(self):
        self.deploy()
        self.check(deployed="a" * 40, available=True, error="git broke")
        st = selfupdate.status()
        self.assertFalse(st["available"])
        self.assertEqual(st["check_error"], "git broke")

    def test_badge_admins_only(self):
        self.deploy()
        self.check(deployed="a" * 40, head="b" * 40, available=True, count=1, commits=[])
        self.assertIn("update-badge", selfupdate.badge(Req()))
        self.assertEqual(selfupdate.badge(Req(role="user")), "")
        self.assertEqual(selfupdate.badge(Req(role=None)), "")

    def test_running_shows_and_goes_stale(self):
        self.deploy()
        self.put(os.path.join(self.state, "run.json"), {"state": "running", "started_at": now_iso()})
        self.assertIn("Updating", selfupdate.badge(Req()))
        self.put(os.path.join(self.state, "run.json"), {"state": "running", "started_at": now_iso(-3 * 3600)})
        self.assertEqual(selfupdate.status()["run"]["state"], "failed")

    def test_asset_version_changes_per_deploy(self):
        self.assertEqual(selfupdate.asset_version(), "0")
        self.deploy()
        v1 = selfupdate.asset_version()
        self.put(config.CFG["deployed_file"], {"commit": "a" * 40, "short": "aaaaaaa", "at": "2026-11-05T01:02:03+00:00"})
        self.assertNotEqual(selfupdate.asset_version(), v1)
        self.assertTrue(selfupdate.asset_version().isalnum())

    def test_page_links_static_files_with_the_version(self):
        self.deploy()
        page = respond.page(Req(), "t", "")[2].decode()
        self.assertIn("style.css?v=" + selfupdate.asset_version(), page)
        self.assertIn("app.js?v=" + selfupdate.asset_version(), page)


class RoutesTest(Base):
    def setUp(self):
        super().setUp()
        server.load_routes()
        self.auth = server.auth

    def tearDown(self):
        server.auth = self.auth
        super().tearDown()

    def call(self, method, role="admin"):
        server.auth = FakeAuth(role)
        status, _, body = server.dispatch(Req(method, role=role))
        return status, (json.loads(body) if body.startswith(b"{") else body)

    def test_admin_only(self):
        self.assertEqual(self.call("GET", role="user")[0], 403)
        self.assertEqual(self.call("POST", role="user")[0], 403)

    def test_post_needs_setup(self):
        status, d = self.call("POST")
        self.assertEqual(status, 409)
        self.assertFalse(d["ok"])

    def test_post_writes_the_request(self):
        self.deploy()
        status, d = self.call("POST")
        self.assertEqual(status, 200, d)
        path = os.path.join(self.root, "manager", "update-request.json")
        with open(path) as f:
            req = json.load(f)
        self.assertEqual(req["by"], "u")
        self.assertEqual(req["at"], d["requested_at"])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_post_refused_while_running(self):
        self.deploy()
        self.put(os.path.join(self.state, "run.json"), {"state": "running", "started_at": now_iso()})
        self.assertEqual(self.call("POST")[0], 409)


class CheckScriptTest(unittest.TestCase):
    """The helper that runs as the clone's owner, against a throwaway git repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "trunk")
        self.commit("one")
        spec = importlib.util.spec_from_loader("update_check", loader=None)
        mod = importlib.util.module_from_spec(spec)
        with open(CHECK_SCRIPT) as f:
            exec(compile(f.read(), CHECK_SCRIPT, "exec"), mod.__dict__)
        self.mod = mod
        self.deployed = os.path.join(self.tmp.name, "deployed.json")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True,
                              env=env).stdout.strip()

    def commit(self, msg):
        with open(os.path.join(self.repo, "f"), "a") as f:
            f.write(msg + "\n")
        self.git("add", "f")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def mark_deployed(self):
        with open(self.deployed, "w") as f:
            json.dump({"commit": self.git("rev-parse", "HEAD")}, f)

    def test_up_to_date(self):
        self.mark_deployed()
        res = self.mod.check(self.repo, self.deployed)
        self.assertFalse(res["available"])

    def test_new_commits(self):
        self.mark_deployed()
        self.commit("two")
        self.commit("three")
        res = self.mod.check(self.repo, self.deployed)
        self.assertTrue(res["available"])
        self.assertEqual(res["count"], 2)
        self.assertEqual([c["subject"] for c in res["commits"]], ["three", "two"])

    def test_diverged_after_reset(self):
        self.commit("two")
        self.mark_deployed()
        self.git("reset", "-q", "--hard", "HEAD~1")
        self.commit("other")
        res = self.mod.check(self.repo, self.deployed)
        self.assertTrue(res["available"] and res["diverged"])
        self.assertEqual(res["commits"][0]["subject"], "other")

    def test_write_is_group_readable_only(self):
        out = os.path.join(self.tmp.name, "check.json")
        self.mod.write(out, {"x": 1})
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
