import json
import os
import stat
import tempfile
import unittest

import config
import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        config.CFG["data_root"] = self.dir.name
        os.makedirs(os.path.join(self.dir.name, "manager"))

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip_and_mode(self):
        p = os.path.join(self.dir.name, "a.json")
        store.write_json(p, {"x": 1}, mode=0o600)
        self.assertEqual(store.read_json(p), {"x": 1})
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertFalse(os.path.exists(p + ".tmp"))

    def test_read_default(self):
        self.assertEqual(store.read_json("/nonexistent/x", "d"), "d")

    def test_audit_lines(self):
        store.audit("admin", "1.2.3.4", "login", "ok")
        store.audit("admin", "1.2.3.4", "logout")
        with open(os.path.join(self.dir.name, "manager", "audit.log")) as f:
            lines = [json.loads(l) for l in f]
        self.assertEqual([l["action"] for l in lines], ["login", "logout"])


if __name__ == "__main__":
    unittest.main()
