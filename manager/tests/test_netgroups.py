import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

import config
import netgroups_store as ns
import netgroups_ui as ui
import server
import store
import vms  # noqa: F401  (must load before vms_pages)
import vms_pages

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "..", "..", "tools", "fake-netapply.py")
ADMIN = {"name": "admin", "role": "admin", "vms": []}


class Req:
    def __init__(self, body=b"", query=None):
        self.user, self.ip, self.session = ADMIN, "127.0.0.1", {"csrf": "tok"}
        self._body, self.query = body, query or {}

    def body(self):
        return self._body


def form(**kw):
    pairs = []
    for k, v in kw.items():
        for item in (v if isinstance(v, list) else [v]):
            pairs.append((k, item))
    return Req(urllib.parse.urlencode(pairs).encode())


def put(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


LAB = {"bridge": "br-lab", "cidr": "10.86.0.0/24", "egress": "none", "egress_allow": [], "dhcp": False}
WEB = {"bridge": "br-web", "cidr": "10.86.1.0/24", "egress": "nat", "egress_allow": ["1.1.1.1"], "dhcp": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.tmp.name
        self.etc, self.run, self.sysnet = (os.path.join(r, d) for d in ("etc", "run", "sysnet"))
        for d in ("etc", "run", "sysnet", "vmdefs", "manager"):
            os.makedirs(os.path.join(r, d))
        self.saved = (dict(config.CFG), config.NETGROUPS_PATH, config.NETPOLICY_PATH, config.NETAPPLY_RESULT, ns.SYSNET)
        config.CFG.pop("netgroups", None)
        config.CFG["data_root"] = r
        config.NETGROUPS_PATH = os.path.join(self.etc, "netgroups.json")
        config.NETPOLICY_PATH = os.path.join(self.etc, "netpolicy.json")
        config.NETAPPLY_RESULT = os.path.join(self.run, "netapply.json")
        ns.SYSNET = self.sysnet
        config._cache.clear()
        self.request = os.path.join(r, "manager", "netgroups-request.json")

    def tearDown(self):
        cfg, config.NETGROUPS_PATH, config.NETPOLICY_PATH, config.NETAPPLY_RESULT, ns.SYSNET = self.saved
        config.CFG.clear()
        config.CFG.update(cfg)
        config._cache.clear()
        self.tmp.cleanup()

    def state(self, groups=None, routes=()):
        put(config.NETGROUPS_PATH, groups if groups is not None else {"lab": LAB, "web": WEB})
        put(config.NETPOLICY_PATH, {"group_routes": [list(x) for x in routes], "nat_dns": ["9.9.9.9"]})

    def sent(self):
        with open(self.request) as f:
            return json.load(f)

    def answer(self, ok=True, message="Applied", warnings=()):
        put(config.NETAPPLY_RESULT, {"id": self.sent()["id"], "ok": ok, "message": message,
                                     "warnings": list(warnings), "ts": 1})

    def vm(self, name, groups):
        put(os.path.join(self.tmp.name, "vmdefs", name + ".json"), {"allowed_groups": groups})


class LiveConfigTest(Base):
    def test_reads_the_applied_file_and_notices_changes(self):
        self.assertEqual(config.netgroups(), {})
        self.state({"lab": LAB})
        self.assertEqual(config.netgroups()["lab"]["bridge"], "br-lab")
        self.state({"lab": LAB, "web": WEB})
        self.assertEqual(sorted(config.netgroups()), ["lab", "web"])

    def test_damaged_file_means_no_groups_and_policy_has_defaults(self):
        with open(config.NETGROUPS_PATH, "w") as f:
            f.write("not json")
        self.assertEqual(config.netgroups(), {})
        self.assertEqual(config.netpolicy(), {"group_routes": [], "nat_dns": ["1.1.1.1"]})

    def test_policy_is_read(self):
        self.state(routes=[("lab", "web")])
        self.assertEqual(config.netpolicy(), {"group_routes": [["lab", "web"]], "nat_dns": ["9.9.9.9"]})

    def test_a_test_override_in_cfg_wins(self):
        self.state()
        config.CFG["netgroups"] = {"x": {"bridge": "br-x"}}
        self.assertEqual(list(config.netgroups()), ["x"])

    def test_an_old_manager_json_cannot_shadow_the_live_file(self):
        p = os.path.join(self.tmp.name, "old.json")
        put(p, {"netgroups": {"stale": {}}, "data_root": "/x"})
        old = config._PATH
        config._PATH = p
        try:
            self.assertNotIn("netgroups", config.load())
        finally:
            config._PATH = old


class ChecksTest(Base):
    def test_suggestions_skip_networks_in_use(self):
        self.assertEqual(ns.suggest_subnets({}), ["10.86.0.0/24", "10.86.1.0/24", "10.86.2.0/24"])
        self.assertEqual(ns.suggest_subnets({"lab": LAB, "web": WEB}, 1), ["10.86.2.0/24"])
        self.assertEqual(ns.suggest_subnets({"big": {"cidr": "10.86.0.0/16"}}), [])

    def test_parse_cidr(self):
        self.assertEqual(ns.parse_cidr(" 192.168.5.0/24 ", {}), "192.168.5.0/24")
        self.assertEqual(ns.parse_cidr("172.16.0.0/29", {}), "172.16.0.0/29")
        for bad in ("8.8.8.0/24", "10.86.0.5/24", "nonsense", "", "10.0.0.0/8", "10.86.0.0/30", "fd00::/64",
                    "172.32.0.0/24", "<script>"):
            with self.assertRaises(ns.NetError, msg=bad):
                ns.parse_cidr(bad, {})
        with self.assertRaises(ns.NetError) as cm:
            ns.parse_cidr("10.86.0.128/25", {"lab": LAB})
        self.assertIn("lab", str(cm.exception))

    def test_guest_help(self):
        h = ns.guest_help("10.86.1.0/24", True, ["9.9.9.9"])
        self.assertEqual((h["gateway"], h["example"], h["mask"], h["dns"]), ("10.86.1.1", "10.86.1.10", "255.255.255.0", "9.9.9.9"))
        self.assertEqual(ns.guest_help("10.86.1.0/24", False, ["9.9.9.9"])["dns"], "")
        self.assertEqual(ns.guest_help("10.86.1.0/29", False, [])["example"], "10.86.1.6")


class RequestTest(Base):
    def test_new_group_has_no_bridge_and_existing_ones_keep_theirs(self):
        self.state({"lab": LAB})
        body = ns.desired(ns.groups(), "web", {"cidr": "10.86.1.0/24", "egress": "nat", "dhcp": True}, ["lab"])
        self.assertEqual(body["netgroups"]["lab"]["bridge"], "br-lab")
        self.assertNotIn("bridge", body["netgroups"]["web"])
        self.assertEqual(body["netgroups"]["web"], {"cidr": "10.86.1.0/24", "egress": "nat", "egress_allow": [], "dhcp": True})
        self.assertEqual(body["group_routes"], [["lab", "web"]])
        self.assertEqual(body["nat_dns"], ["9.9.9.9"])      # the applied DNS setting is passed through unchanged

    def test_editing_keeps_the_allow_list_and_replaces_only_this_groups_pairs(self):
        self.state({"lab": LAB, "web": WEB, "dmz": dict(LAB, bridge="br-dmz", cidr="10.86.2.0/24")},
                   routes=[("dmz", "lab"), ("lab", "web")])
        body = ns.desired(ns.groups(), "web", {"cidr": "10.86.1.0/24", "egress": "none", "dhcp": False}, [])
        self.assertEqual(body["netgroups"]["web"]["egress_allow"], ["1.1.1.1"])
        self.assertEqual(body["group_routes"], [["dmz", "lab"]])
        self.assertEqual(body["nat_dns"], ["9.9.9.9"])

    def test_removing_a_group_drops_its_pairs(self):
        self.state(routes=[("lab", "web")])
        body = ns.desired(ns.groups(), "web", {}, [], remove=True)
        self.assertEqual(list(body["netgroups"]), ["lab"])
        self.assertEqual(body["group_routes"], [])

    def test_submit_writes_a_private_request_and_allows_one_at_a_time(self):
        rid = ns.submit({"netgroups": {}, "group_routes": [], "nat_dns": ["1.1.1.1"]})
        self.assertEqual(self.sent()["id"], rid)
        self.assertEqual(stat.S_IMODE(os.stat(self.request).st_mode), 0o600)
        self.assertEqual(ns.status()["state"], "applying")
        with self.assertRaises(ns.NetError):
            ns.submit({"netgroups": {}, "group_routes": [], "nat_dns": []})
        self.answer()
        ns.submit({"netgroups": {}, "group_routes": [], "nat_dns": []})   # allowed again

    def test_status_states(self):
        self.assertEqual(ns.status()["state"], "idle")
        ns.submit({"netgroups": {}, "group_routes": [], "nat_dns": []})
        self.assertEqual(ns.status()["state"], "applying")
        late = ns.status(now=time.time() + ns.APPLY_TIMEOUT + 5)
        self.assertEqual(late["state"], "error")
        self.assertIn("86box-netapply", late["message"])
        self.answer(True, "Applied", ["a warning"])
        self.assertEqual(ns.status(), {"state": "ok", "message": "Applied", "warnings": ["a warning"]})
        self.answer(False, "No such bridge")
        self.assertEqual((ns.status()["state"], ns.status()["message"]), ("error", "No such bridge"))
        put(config.NETAPPLY_RESULT, {"id": "someone-elses", "ok": True, "message": "old"})
        self.assertEqual(ns.status()["state"], "applying")   # an answer to a different request does not count


class PagesTest(Base):
    def test_list_explains_and_shows_the_columns(self):
        self.state(routes=[("lab", "web")])
        self.vm("nt4", ["lab"])
        out = ui.h_list(Req(), None)[2].decode()
        self.assertIn("private virtual switch", out)
        self.assertIn("Through this host", out)
        self.assertIn("10.86.0.1", out)           # the host address guests use as gateway
        self.assertRegex(out, r"<td>lab</td><td>10\.86\.0\.0/24</td>")
        self.assertIn("nt4", out)
        self.assertIn("not created yet", out)
        os.makedirs(os.path.join(self.sysnet, "br-lab", "bridge"))
        self.assertIn("ready", ui.h_list(Req(), None)[2].decode())

    def test_empty_list_says_what_that_means(self):
        out = ui.h_list(Req(), None)[2].decode()
        self.assertIn("No network groups yet", out)
        self.assertIn("/manage/netgroups/new", out)

    def test_banners(self):
        ns.submit({"netgroups": {}, "group_routes": [], "nat_dns": []})
        self.assertIn('id="net-apply"', ui.h_list(Req(), None)[2].decode())
        self.answer(False, "Bridge <b>x</b> failed")
        out = ui.h_list(Req(), None)[2].decode()
        self.assertIn("The last change failed", out)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)
        self.answer(True, "Applied", ["Careful <i>now</i>"])
        out = ui.h_list(Req(query={"done": ["1"]}), None)[2].decode()
        self.assertIn("Done.", out)
        self.assertIn("Careful &lt;i&gt;now&lt;/i&gt;", out)

    def test_new_form_suggests_a_free_network_and_explains_the_guest_side(self):
        self.state({"lab": LAB})
        out = ui.h_new(Req(), None)[2].decode()
        self.assertIn('<option value="10.86.1.0/24" selected>', out)
        self.assertIn("Inside the guest", out)
        self.assertIn("10.86.1.10", out)
        self.assertIn("reach the internet through this host", out)
        self.assertIn("<b>none (no internet)</b>", out)

    def test_edit_form_warns_when_vms_use_the_group_and_forbids_rename(self):
        self.state()
        self.vm("nt4", ["lab"])
        out = ui.h_edit(Req(), re.fullmatch(ui.NAME, "lab"))[2].decode()
        self.assertIn("nt4 use this group", out)
        self.assertIn("cannot be renamed", out)
        self.assertIn("cannot be deleted while boxes use it", out)
        self.assertEqual(ui.h_edit(Req(), re.fullmatch(ui.NAME, "nope"))[0], 404)

    def test_create(self):
        self.state({"lab": LAB})
        status = ui.h_save(form(csrf="tok", mode="create", name="web", choice="10.86.1.0/24", custom="", nat="1",
                                dhcp="1", reach="lab"), None)[0]
        self.assertEqual(status, 303)
        sent = self.sent()
        self.assertEqual(sent["netgroups"]["web"], {"cidr": "10.86.1.0/24", "egress": "nat", "egress_allow": [], "dhcp": True})
        self.assertEqual(sent["netgroups"]["lab"]["bridge"], "br-lab")
        self.assertEqual(sent["group_routes"], [["lab", "web"]])
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", sent["id"]))
        with open(os.path.join(self.tmp.name, "manager", "audit.log")) as f:
            self.assertIn("netgroup.save", f.read())

    def test_create_with_a_custom_network(self):
        ui.h_save(form(mode="create", name="dmz", choice="custom", custom="192.168.50.0/28"), None)
        self.assertEqual(self.sent()["netgroups"]["dmz"]["cidr"], "192.168.50.0/28")

    def test_bad_input_is_refused_and_nothing_is_sent(self):
        self.state({"lab": LAB})
        cases = [dict(name="Bad Name", choice="10.86.1.0/24"), dict(name="lab", choice="10.86.1.0/24"),
                 dict(name="x", choice="custom", custom="8.8.8.0/24"), dict(name="x", choice="10.86.0.0/24"),
                 dict(name="x", choice="10.86.1.0/24", reach="nope"), dict(name="thirteenchars1", choice="10.86.1.0/24")]
        for c in cases:
            status, _, out = ui.h_save(form(mode="create", **c), None)
            self.assertEqual(status, 400, c)
            self.assertIn(b"<form", out)
        self.assertFalse(os.path.exists(self.request))

    def test_values_are_escaped_in_errors(self):
        status, _, out = ui.h_save(form(mode="create", name="<script>", choice="custom", custom="<img onerror=1>"), None)
        self.assertEqual(status, 400)
        self.assertNotIn(b'value="<script>"', out)   # the page has its own theme <script>, so look for the value
        self.assertIn(b"&lt;script&gt;", out)
        self.assertNotIn(b"<img", out)

    def test_edit_changes_only_what_was_asked(self):
        self.state(routes=[("lab", "web")])
        ui.h_save(form(mode="edit", name="web", choice="10.86.1.0/24", custom=""), None)
        sent = self.sent()
        self.assertEqual(sent["netgroups"]["web"]["egress"], "none")
        self.assertEqual(sent["netgroups"]["web"]["bridge"], "br-web")
        self.assertEqual(sent["group_routes"], [])          # the box for lab was left unticked
        self.assertEqual(ui.h_save(form(mode="edit", name="ghost", choice="10.86.9.0/24"), None)[0], 400)

    def test_a_second_change_waits_for_the_first(self):
        self.state({"lab": LAB})
        ui.h_save(form(mode="create", name="web", choice="10.86.1.0/24"), None)
        status, _, out = ui.h_save(form(mode="create", name="dmz", choice="10.86.2.0/24"), None)
        self.assertEqual(status, 400)
        self.assertIn(b"still being applied", out)

    def test_delete_is_refused_while_a_vm_uses_the_group(self):
        self.state()
        self.vm("nt4", ["lab"])
        status, _, out = ui.h_delete(form(confirm="lab"), re.fullmatch(ui.NAME, "lab"))
        self.assertEqual(status, 400)
        self.assertIn(b"nt4", out)
        self.assertFalse(os.path.exists(self.request))

    def test_delete_needs_the_name_typed_and_then_sends_the_rest(self):
        self.state(routes=[("lab", "web")])
        m = re.fullmatch(ui.NAME, "web")
        self.assertEqual(ui.h_delete(form(confirm="wrong"), m)[0], 400)
        self.assertFalse(os.path.exists(self.request))
        self.assertEqual(ui.h_delete(form(confirm="web"), m)[0], 303)
        self.assertEqual(list(self.sent()["netgroups"]), ["lab"])
        self.assertEqual(self.sent()["group_routes"], [])

    def test_routes_are_admin_only_and_registered(self):
        server.load_routes()
        roles = {p.pattern: r for meth, p, h, r in server.ROUTES if "netgroups" in p.pattern}
        self.assertEqual(len(roles), 6)
        self.assertEqual(set(roles.values()), {"admin"})

    def test_status_api(self):
        out = ui.h_status(Req(), None)
        self.assertEqual(json.loads(out[2])["state"], "idle")

    def test_vm_pages_show_subnets_and_a_way_to_manage_groups(self):
        self.assertIn("create one", vms_pages._group_checks(()))
        self.state()
        out = vms_pages._group_checks(["lab"])
        self.assertIn("10.86.0.0/24", out)
        self.assertRegex(out, r'value="lab" checked')
        self.assertNotRegex(out, r'value="web" checked')
        self.assertIn("/manage/netgroups", out)


class FakeApplierTest(Base):
    """The page against the stand-in for the root helper: create, route, edit and delete groups."""

    def apply(self):
        r = subprocess.run([sys.executable, FAKE, self.request, self.etc, self.run], capture_output=True, text=True)
        config._cache.clear()
        return r.returncode

    def test_whole_flow(self):
        ui.h_save(form(mode="create", name="lab", choice="10.86.0.0/24"), None)
        self.assertEqual(ns.status()["state"], "applying")
        self.assertEqual(self.apply(), 0)
        self.assertEqual(ns.status()["state"], "ok")
        self.assertEqual(config.netgroups()["lab"]["bridge"], "br-lab")
        self.assertIn(">lab<", ui.h_list(Req(), None)[2].decode())

        ui.h_save(form(mode="create", name="web", choice="10.86.1.0/24", nat="1", reach="lab"), None)
        self.assertEqual(self.apply(), 0)
        self.assertEqual(ns.reaches("lab"), ["web"])
        self.assertEqual(config.netgroups()["web"]["egress"], "nat")

        ui.h_save(form(mode="edit", name="web", choice="custom", custom="10.86.9.0/24"), None)
        self.assertEqual(self.apply(), 0)
        self.assertEqual(config.netgroups()["web"]["cidr"], "10.86.9.0/24")
        self.assertEqual(config.netgroups()["web"]["bridge"], "br-web")     # the bridge is kept
        self.assertEqual(ns.reaches("lab"), [])                              # the box was left unticked

        self.vm("nt4", ["lab"])
        self.assertEqual(ui.h_delete(form(confirm="lab"), re.fullmatch(ui.NAME, "lab"))[0], 400)
        ui.h_delete(form(confirm="web"), re.fullmatch(ui.NAME, "web"))
        self.assertEqual(self.apply(), 0)
        self.assertEqual(list(config.netgroups()), ["lab"])

    def test_helper_refusal_shows_up_as_an_error_on_the_page(self):
        ui.h_save(form(mode="create", name="lab", choice="10.86.0.0/24"), None)
        self.apply()
        with open(self.request) as f:
            req = json.load(f)
        req["netgroups"]["lab"]["cidr"] = "garbage"
        put(self.request, req)
        self.assertEqual(self.apply(), 1)
        self.assertEqual(ns.status()["state"], "error")
        self.assertIn("The last change failed", ui.h_list(Req(), None)[2].decode())
        self.assertEqual(config.netgroups()["lab"]["cidr"], "10.86.0.0/24")   # nothing was changed


if __name__ == "__main__":
    unittest.main()
