import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import cloudflare
import cloudflare_cli
import config
import edge
import secrets_store

TOKEN = "cf-token-SECRET-9876"
ZONES = [{"id": "z1", "name": "example.com", "account_id": "acc", "account_name": "A"}]


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "manager"))
        self.saved = dict(config.CFG)
        config.CFG.update(data_root=self.tmp.name)

    def tearDown(self):
        config.CFG.clear()
        config.CFG.update(self.saved)
        self.tmp.cleanup()

    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                rc = cloudflare_cli.main(list(argv))
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (print(e.code, file=err) or 1)
        return rc, out.getvalue(), err.getvalue()

    def test_token_is_checked_then_saved_from_stdin(self):
        with mock.patch.object(cloudflare, "list_zones", side_effect=cloudflare.CFError("Invalid token")):
            rc, _, err = self.run_cli("token", stdin=TOKEN + "\n")
        self.assertEqual((rc, secrets_store.get(edge.TOKEN)), (1, ""))
        self.assertIn("Invalid token", err)
        with mock.patch.object(cloudflare, "list_zones", return_value=ZONES):
            rc, out, _ = self.run_cli("token", stdin=TOKEN + "\n")
        self.assertEqual(rc, 0)
        self.assertIn("example.com", out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(secrets_store.get(edge.TOKEN), TOKEN)

    def test_setup_without_token(self):
        rc, _, err = self.run_cli("setup", "--zones", "example.com")
        self.assertEqual(rc, 1)
        self.assertIn("No Cloudflare API token", err)

    def test_setup_passes_choices_to_sync(self):
        secrets_store.put(edge.TOKEN, TOKEN)
        with mock.patch.object(cloudflare, "list_zones", return_value=ZONES), \
                mock.patch.object(edge, "sync", return_value=True) as sync:
            rc, out, _ = self.run_cli("setup", "--zones", "example.com", "--panel", "box", "--panel-zone",
                                      "example.com", "--no-tunnel", "--replace-dns")
        self.assertEqual(rc, 0)
        state, token, replace = sync.call_args[0]
        self.assertEqual((state["panel"], state["tunnel"], token, replace), ("box.example.com", False, TOKEN, True))

    def test_setup_rejects_bad_choices(self):
        secrets_store.put(edge.TOKEN, TOKEN)
        with mock.patch.object(cloudflare, "list_zones", return_value=ZONES), \
                mock.patch.object(edge, "sync") as sync:
            rc, _, err = self.run_cli("setup", "--zones", "nope.net")
        self.assertEqual(rc, 1)
        self.assertIn("cannot see nope.net", err)
        sync.assert_not_called()

    def test_panel_url(self):
        self.assertEqual(self.run_cli("panel-url")[1], "")
        edge.save({"zones": {"example.com": {"id": "z1"}}, "panel": "box.example.com"})
        self.assertEqual(self.run_cli("panel-url")[1], "https://box.example.com/\n")


if __name__ == "__main__":
    unittest.main()
