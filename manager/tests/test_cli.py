import contextlib
import io
import unittest
from unittest import mock

import auth
import cli
import vms


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.defs = mock.patch.object(vms, "all_defs", return_value={"a-vm": {}, "b-vm": {}})
        self.defs.start()

    def tearDown(self):
        self.defs.stop()

    def test_list(self):
        st = lambda n: {"name": n, "state": "running", "nic": "ok", "guard": ""}
        with mock.patch.object(vms, "status", side_effect=st):
            rc, out, _ = run(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("a-vm", out)
        self.assertIn("NAME", out)

    def test_actions_map_to_unit_verbs(self):
        for verb, unit_verb in (("start", "start"), ("stop", "stop"), ("restart", "restart")):
            with mock.patch("units.run", return_value=(True, "")) as run_unit:
                rc, out, _ = run([verb, "a-vm"])
            self.assertEqual(rc, 0)
            run_unit.assert_called_once_with(unit_verb, "box@a-vm.service")

    def test_unknown_vm_and_usage(self):
        self.assertEqual(run(["start", "nope"])[0], 1)
        self.assertEqual(run([])[0], 2)
        self.assertEqual(run(["delete", "a-vm"])[0], 2)

    def test_failure_is_reported(self):
        with mock.patch("units.run", return_value=(False, "denied")):
            rc, _, err = run(["stop", "a-vm"])
        self.assertEqual(rc, 1)
        self.assertIn("denied", err)


    def test_check_password_reads_stdin(self):
        users = {"users": {"admin": {"hash": auth.hash_password("right-password-1"), "role": "admin"}}}
        with mock.patch.object(auth, "_load", return_value=users):
            with mock.patch("sys.stdin", io.StringIO("right-password-1\n")):
                self.assertEqual(run(["check-password", "admin"])[0], 0)
            with mock.patch("sys.stdin", io.StringIO("wrong-password-1\n")):
                self.assertEqual(run(["check-password", "admin"])[0], 1)
            with mock.patch("sys.stdin", io.StringIO("right-password-1\n")):
                self.assertEqual(run(["check-password", "nobody"])[0], 1)

    def test_set_password_reads_stdin(self):
        with mock.patch.object(auth, "_load_sessions") as loaded, \
                mock.patch.object(auth, "set_password") as setpw, mock.patch("sys.stdin", io.StringIO("new-password-12\n")):
            rc, out, _ = run(["set-password", "admin"])
        self.assertEqual(rc, 0)
        setpw.assert_called_once_with("admin", "new-password-12")
        loaded.assert_called_once()
        with mock.patch.object(auth, "_load_sessions"), \
                mock.patch.object(auth, "set_password", side_effect=auth.AuthError("too short")), \
                mock.patch("sys.stdin", io.StringIO("x\n")):
            rc, _, err = run(["set-password", "admin"])
        self.assertEqual(rc, 1)
        self.assertIn("too short", err)


if __name__ == "__main__":
    unittest.main()
