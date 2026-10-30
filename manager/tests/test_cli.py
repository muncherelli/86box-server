import contextlib
import io
import unittest
from unittest import mock

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
            run_unit.assert_called_once_with(unit_verb, "vm@a-vm.service")

    def test_unknown_vm_and_usage(self):
        self.assertEqual(run(["start", "nope"])[0], 1)
        self.assertEqual(run([])[0], 2)
        self.assertEqual(run(["delete", "a-vm"])[0], 2)

    def test_failure_is_reported(self):
        with mock.patch("units.run", return_value=(False, "denied")):
            rc, _, err = run(["stop", "a-vm"])
        self.assertEqual(rc, 1)
        self.assertIn("denied", err)


if __name__ == "__main__":
    unittest.main()
