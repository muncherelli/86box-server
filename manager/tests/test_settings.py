import json
import os
import re
import tempfile
import unittest
from unittest import mock

import config
import settings
import vms


class Req:
    user = {"name": "admin", "role": "admin", "vms": []}
    session = {"csrf": "tok"}


def match(name):
    return re.fullmatch(settings.ROUTES[0][1], "/manage/boxes/%s/settings" % name)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        config.CFG["data_root"] = self.dir.name
        config.CFG["netgroups"] = {"web": {"bridge": "br-web"}}
        os.makedirs(os.path.join(self.dir.name, "boxdefs"))
        with open(os.path.join(self.dir.name, "boxdefs", "vm1.json"), "w") as f:
            json.dump({"name": "vm1", "allowed_groups": ["web"]}, f)
        auth = mock.Mock()
        auth.can_use_vm.return_value = True
        p = mock.patch.object(vms, "_auth", return_value=auth)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.dir.cleanup()

    def active(self, **states):
        return mock.patch("units.is_active", side_effect=lambda u: states.get(u, False))

    def test_stopped_page_has_open_button_and_groups(self):
        with self.active():
            status, _, body = settings.page(Req, match("vm1"))
        self.assertEqual(status, 200)
        self.assertIn(b"Open Settings", body)
        self.assertIn(b"br-web", body)

    def test_running_page_shows_menu_path(self):
        with self.active(**{"box@vm1.service": True}):
            _, _, body = settings.page(Req, match("vm1"))
        self.assertIn(b"Tools &gt; Settings", body)
        self.assertNotIn(b"Open Settings", body)

    def test_open_starts_unit_and_redirects_to_console(self):
        with self.active(), mock.patch("units.run", return_value=(True, "")) as run:
            status, headers, _ = settings.open_settings(Req, match("vm1"))
        run.assert_called_once_with("start", "box-settings@vm1.service")
        self.assertEqual(status, 303)
        self.assertIn("token%3Dvm1", headers["Location"])

    def test_open_while_running_does_not_start(self):
        with self.active(**{"box@vm1.service": True}), mock.patch("units.run") as run:
            status, headers, _ = settings.open_settings(Req, match("vm1"))
        run.assert_not_called()
        self.assertEqual(headers["Location"], "/manage/boxes/vm1/settings")

    def test_unknown_vm_404_and_start_failure(self):
        with self.active():
            self.assertEqual(settings.page(Req, match("nope"))[0], 404)
            with mock.patch("units.run", return_value=(False, "boom")):
                self.assertEqual(settings.open_settings(Req, match("vm1"))[0], 500)


if __name__ == "__main__":
    unittest.main()
