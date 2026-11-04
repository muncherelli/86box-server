import datetime
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import cloudflare
import config
import edge
import secrets_store
import sites
import store

ZONE = "example.com"
HOST = "panel.example.com"
TOKEN = "cf-token-abcd1234"
TOKEN_ZONES = [{"id": "z1", "name": ZONE, "account_id": "acc", "account_name": "A"},
               {"id": "z2", "name": "other.org", "account_id": "acc", "account_name": "A"}]
SITE = {"host": "legacy.example.com", "vm": "nt4-web", "guest_ip": "10.86.1.10", "port": 80}


def write_fake_lego(path, cert_src, key_text="KEY\n", fail=False):
    """A stand-in for lego: records its arguments and env, then stores a certificate like lego does."""
    with open(path, "w") as f:
        f.write("""#!/usr/bin/env python3
import json, os, shutil, sys
args = sys.argv[1:]
log = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "lego-calls.json")
calls = json.load(open(log)) if os.path.exists(log) else []
calls.append({"args": args, "token": os.environ.get("CLOUDFLARE_DNS_API_TOKEN", "")})
json.dump(calls, open(log, "w"))
if %r:
    print("acme: error: 403 :: some failure for " + os.environ["CLOUDFLARE_DNS_API_TOKEN"], file=sys.stderr)
    sys.exit(1)
root = args[args.index("--path") + 1]
name = args[args.index("--cert.name") + 1]
d = os.path.join(root, "certificates")
os.makedirs(d, exist_ok=True)
shutil.copy(%r, os.path.join(d, name + ".crt"))
open(os.path.join(d, name + ".key"), "w").write(%r)
""" % (fail, cert_src, key_text))
    os.chmod(path, 0o755)


class FakeCF:
    """In-memory Cloudflare for the sync steps."""
    def __init__(self, records=None, tunnel=None):
        self.records = list(records or [])
        self.tunnel = tunnel
        self.config = {"warp-routing": {"enabled": False},
                       "ingress": [{"hostname": "stale.example.com", "service": "http://localhost:9"},
                                   {"service": "http_status:404"}]}
        self.deleted = []
        self.calls = []

    def patch(self, test):
        for name in ("find_tunnel", "create_tunnel", "get_config", "put_config", "tunnel_token", "find_records",
                     "create_cname", "delete_record", "clean_connections", "delete_tunnel"):
            p = mock.patch.object(cloudflare, name, getattr(self, name))
            p.start()
            test.addCleanup(p.stop)
        return self

    def find_tunnel(self, token, acct, name):
        return self.tunnel if self.tunnel and self.tunnel["name"] == name else None

    def create_tunnel(self, token, acct, name):
        self.tunnel = {"id": "tun-1", "name": name}
        self.calls.append(("create", name))
        return self.tunnel

    def get_config(self, token, acct, tid):
        return self.config

    def put_config(self, token, acct, tid, cfg):
        self.config = cfg

    def tunnel_token(self, token, acct, tid):
        return "TUNNEL-SECRET"

    def find_records(self, token, zone, name):
        return [r for r in self.records if r["name"] == name]

    def create_cname(self, token, zone, name, target, comment=""):
        self.records.append({"id": "new-" + name, "name": name, "type": "CNAME", "content": target, "zone": zone})

    def delete_record(self, token, zone, rid):
        self.deleted.append(rid)
        self.records = [r for r in self.records if r["id"] != rid]

    def clean_connections(self, token, acct, tid):
        self.calls.append(("clean", tid))

    def delete_tunnel(self, token, acct, tid):
        self.calls.append(("delete", tid))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for d in ("manager", "certs", "panel.d", "sites.d", "boxdefs", "bin"):
            os.makedirs(os.path.join(self.root, d))
        self.certs = os.path.join(self.root, "certs")
        self.panel_dir = os.path.join(self.root, "panel.d")
        self.lego = os.path.join(self.root, "bin", "lego")
        self.saved = dict(config.CFG)
        config.CFG.update(data_root=self.root, certs_dir=self.certs, panel_dir=self.panel_dir, lego_bin=self.lego,
                          sites_dir=os.path.join(self.root, "sites.d"), publish_domains=[],
                          netgroups={"web": {"bridge": "br-web", "cidr": "10.86.1.0/24"}})
        store.write_json(os.path.join(self.root, "boxdefs", "nt4-web.json"), {"allowed_groups": ["web"]})
        self.cert = os.path.join(self.root, "cert.pem")
        if shutil.which("openssl"):
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                            "-nodes", "-days", "60", "-subj", "/CN=*." + ZONE,
                            "-keyout", os.path.join(self.root, "k.pem"), "-out", self.cert],
                           check=True, capture_output=True)
        else:
            with open(self.cert, "w") as f:
                f.write("CERT\n")
        write_fake_lego(self.lego, self.cert)
        self.run = mock.patch("units.run", return_value=(True, "")).start()
        self.validate = mock.patch("sites.caddy_validate", return_value=(True, "valid")).start()
        mock.patch("socket.gethostname", return_value="Lab-01.home").start()
        edge.JOB.update(kind="", running=False, steps=[], done="", ok=None, again=False)
        self.lines = []

    def tearDown(self):
        mock.patch.stopall()
        config.CFG.clear()
        config.CFG.update(self.saved)
        self.tmp.cleanup()

    def lego_calls(self):
        path = os.path.join(self.root, "bin", "lego-calls.json")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return json.load(f)

    def state(self, **kw):
        return edge.configure(edge.load(), TOKEN_ZONES, [ZONE], kw.pop("panel_name", "panel"),
                              kw.pop("panel_zone", ZONE), kw.pop("email", ""), kw.pop("staging", True),
                              kw.pop("tunnel", True))

    def sync(self, state, replace=False, token=TOKEN):
        return edge.sync(state, token, replace, report=edge.PrintReport(self.lines.append))

    def step(self, name):
        for line in self.lines:
            parts = line.split(None, 2)
            if parts[0] == name and not parts[1].startswith("..."):
                return parts[1], (parts[2] if len(parts) > 2 else "")
        return None, ""


class NamesTest(Base):
    def test_hostname_for(self):
        self.assertEqual(edge.hostname_for("Box", ZONE), "box.example.com")
        self.assertEqual(edge.hostname_for("@", ZONE), ZONE)
        for bad in ("-x", "a_b", "a.b", "x y", "a\nb"):
            with self.assertRaises(ValueError):
                edge.hostname_for(bad, ZONE)

    def test_zone_for_covers_one_level(self):
        zones = {ZONE: {}}
        self.assertEqual(config.zone_for(ZONE, zones), ZONE)
        self.assertEqual(config.zone_for("a.example.com", zones), ZONE)
        self.assertEqual(config.zone_for("a.b.example.com", zones), "")
        self.assertEqual(config.zone_for("badexample.com", zones), "")
        self.assertEqual(config.zone_for("a.example.com", {}), "")

    def test_render_panel_uses_the_zone_certificate(self):
        self.assertEqual(edge.render_panel(HOST, ZONE),
                         "# managed by 86server: do not edit\n%s {\n\ttls %s/%s.crt %s/%s.key\n"
                         "\timport b86_manager {http.request.scheme}\n}\n" % (HOST, self.certs, ZONE, self.certs, ZONE))
        with self.assertRaises(ValueError):
            edge.render_panel("bad host {", ZONE)

    def test_tunnel_is_named_after_the_server(self):
        self.assertEqual(edge.tunnel_name(), "86server-lab-01")


class ConfigureTest(Base):
    def test_choices(self):
        s = edge.configure({}, TOKEN_ZONES, ["other.org"], "panel", ZONE, "a@example.org", False, False)
        self.assertEqual(sorted(s["zones"]), [ZONE, "other.org"])  # the panel's zone is added
        self.assertEqual(s["zones"][ZONE], {"id": "z1", "account_id": "acc"})
        self.assertEqual((s["panel"], s["email"], s["staging"], s["tunnel"]), (HOST, "a@example.org", False, False))
        self.assertEqual(edge.configure({}, TOKEN_ZONES, [ZONE])["panel"], "")

    def test_refusals(self):
        for args in ((["nope.net"],), ([ZONE], "a.b", ZONE), ([ZONE], "p", ZONE, "not-an-email")):
            with self.assertRaises(ValueError):
                edge.configure({}, TOKEN_ZONES, *args)

    def test_cannot_drop_a_zone_a_site_uses(self):
        old = {"zones": {ZONE: {"id": "z1"}}}
        sites.save_sites([SITE])
        with self.assertRaisesRegex(ValueError, "legacy.example.com"):
            edge.configure(old, TOKEN_ZONES, ["other.org"])

    def test_keeps_certificate_info(self):
        old = {"zones": {ZONE: {"id": "z1", "cert": {"expires": "x"}}}}
        self.assertEqual(edge.configure(old, TOKEN_ZONES, [ZONE])["zones"][ZONE]["cert"], {"expires": "x"})


class SyncTest(Base):
    def test_full_setup_with_panel_and_site(self):
        cf = FakeCF().patch(self)
        state = self.state()
        edge.save(state)  # zone known, so the site is allowed
        sites.save_sites([SITE, dict(SITE, host="off.example.com", enabled=False),
                          dict(SITE, host="plain.example.com", scheme="http")])
        self.assertTrue(self.sync(state), self.lines)
        self.assertEqual([self.step(s)[0] for s in edge.STEPS], ["ok"] * 5, self.lines)
        # lego: one wildcard certificate for the zone, staging, token only in the environment
        call, = self.lego_calls()
        args = call["args"]
        self.assertEqual(args[0], "run")
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--domains"], ["*." + ZONE, ZONE])
        self.assertEqual(args[args.index("--cert.name") + 1], ZONE)
        self.assertIn("letsencrypt-staging", args)
        self.assertEqual(args[args.index("--dns.resolvers") + 1], "1.1.1.1:53,1.0.0.1:53")
        self.assertNotIn("--email", args)
        self.assertEqual(call["token"], TOKEN)
        self.assertNotIn(TOKEN, " ".join(args))
        for ext in (".crt", ".key"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.certs, ZONE + ext)).st_mode), 0o640)
        # caddy: the panel site and the published sites, both with the zone certificate
        with open(os.path.join(self.panel_dir, "panel.caddy")) as f:
            self.assertEqual(f.read(), edge.render_panel(HOST, ZONE))
        with open(os.path.join(config.CFG["sites_dir"], "legacy.example.com.caddy")) as f:
            self.assertIn("\ttls %s/%s.crt" % (self.certs, ZONE), f.read())
        # tunnel: named after the server, exactly our routes (stale ones gone), other settings kept
        self.assertEqual(cf.calls, [("create", "86server-lab-01")])
        self.assertEqual(cf.config["warp-routing"], {"enabled": False})
        ingress = cf.config["ingress"]
        self.assertEqual([r.get("hostname") for r in ingress], [HOST, "legacy.example.com", "plain.example.com", None])
        self.assertEqual(ingress[0]["service"], "https://localhost:443")
        self.assertEqual(ingress[0]["originRequest"], {"originServerName": HOST, "httpHostHeader": HOST})
        self.assertEqual(ingress[2]["service"], "http://localhost:80")
        env = os.path.join(self.root, "manager", "cloudflared.env")
        with open(env) as f:
            self.assertEqual(f.read(), "TUNNEL_TOKEN=TUNNEL-SECRET\n")
        self.assertEqual(stat.S_IMODE(os.stat(env).st_mode), 0o600)
        # dns: one proxied CNAME per route
        self.assertEqual(sorted(r["name"] for r in cf.records), ["legacy.example.com", HOST, "plain.example.com"])
        self.assertTrue(all(r["content"] == "tun-1.cfargotunnel.com" for r in cf.records))
        self.run.assert_any_call("restart", "86server-cloudflared.service")
        s = edge.load()
        self.assertEqual(s["dns"], ["legacy.example.com", HOST, "plain.example.com"])
        self.assertEqual((s["tunnel_id"], s["tunnel_name"]), ("tun-1", "86server-lab-01"))
        self.assertIn(HOST, config.manager_hosts())
        self.assertTrue(config.tunnel_on())
        if shutil.which("openssl"):
            self.assertGreater(edge.days_left(s["zones"][ZONE]["cert"]), 58)

    def test_second_run_reuses_certificate_tunnel_and_records(self):
        cf = FakeCF().patch(self)
        self.assertTrue(self.sync(self.state()))
        self.assertTrue(self.sync(edge.load()), self.lines)
        if shutil.which("openssl"):
            self.assertEqual(len(self.lego_calls()), 1)
        self.assertEqual(cf.calls, [("create", "86server-lab-01")])
        self.assertEqual(len(cf.records), 1)

    def test_staging_change_gets_a_new_certificate(self):
        FakeCF().patch(self)
        self.assertTrue(self.sync(self.state()))
        self.assertTrue(self.sync(self.state(staging=False)))
        self.assertEqual(len(self.lego_calls()), 2)
        self.assertIn("letsencrypt", self.lego_calls()[1]["args"])

    def test_removed_site_loses_its_route_and_record(self):
        cf = FakeCF().patch(self)
        edge.save(self.state())
        sites.save_sites([SITE])
        self.assertTrue(self.sync(edge.load()))
        sites.save_sites([])
        self.assertTrue(self.sync(edge.load()))
        self.assertEqual([r["name"] for r in cf.records], [HOST])
        self.assertEqual(edge.load()["dns"], [HOST])
        self.assertEqual([r.get("hostname") for r in cf.config["ingress"]], [HOST, None])

    def test_existing_record_is_not_overwritten(self):
        cf = FakeCF(records=[{"id": "a1", "name": HOST, "type": "A", "content": "192.0.2.1"}]).patch(self)
        self.assertFalse(self.sync(self.state()))
        state, msg = self.step("dns")
        self.assertEqual(state, "error")
        self.assertIn("A 192.0.2.1", msg)
        self.assertEqual(cf.deleted, [])
        self.assertIsNone(self.step("cloudflared")[0])

    def test_replace_existing_record_when_asked(self):
        cf = FakeCF(records=[{"id": "a1", "name": HOST, "type": "A", "content": "192.0.2.1"}]).patch(self)
        self.assertTrue(self.sync(self.state(), replace=True))
        self.assertEqual(cf.deleted, ["a1"])

    def test_no_tunnel_is_lan_only_with_real_certificates(self):
        cf = FakeCF().patch(self)
        self.assertTrue(self.sync(self.state(tunnel=False, staging=False, email="a@example.org")))
        self.assertEqual(self.step("tunnel"), ("ok", "tunnel off"))
        args = self.lego_calls()[0]["args"]
        self.assertIn("letsencrypt", args)
        self.assertEqual(args[args.index("--email") + 1], "a@example.org")
        self.assertIsNone(cf.tunnel)
        self.assertTrue(os.path.exists(os.path.join(self.panel_dir, "panel.caddy")))
        self.run.assert_any_call("stop", "86server-cloudflared.service")

    def test_turning_the_tunnel_off_and_deleting_it(self):
        cf = FakeCF().patch(self)
        self.assertTrue(self.sync(self.state()))
        self.assertTrue(self.sync(dict(edge.load(), tunnel=False)))
        self.assertEqual(cf.records, [])
        self.assertEqual(cf.config["ingress"], [{"service": "http_status:404"}])
        self.assertFalse(os.path.exists(os.path.join(self.root, "manager", "cloudflared.env")))
        self.assertTrue(os.path.exists(os.path.join(self.panel_dir, "panel.caddy")))  # still served on the LAN
        self.assertFalse(config.tunnel_on())
        self.assertEqual(edge.delete_tunnel(edge.load(), TOKEN), "tunnel deleted")
        self.assertEqual(cf.calls[-2:], [("clean", "tun-1"), ("delete", "tun-1")])
        self.assertNotIn("tunnel_id", edge.load())

    def test_no_panel_removes_the_panel_site(self):
        FakeCF().patch(self)
        self.assertTrue(self.sync(self.state()))
        self.assertTrue(self.sync(self.state(panel_name="", panel_zone="")))
        self.assertFalse(os.path.exists(os.path.join(self.panel_dir, "panel.caddy")))
        self.assertNotIn(HOST, config.manager_hosts())

    def test_lego_failure_stops_and_hides_token(self):
        FakeCF().patch(self)
        write_fake_lego(self.lego, self.cert, fail=True)
        self.assertFalse(self.sync(self.state()))
        state, msg = self.step("certificates")
        self.assertEqual(state, "error")
        self.assertIn("some failure", msg)
        self.assertNotIn(TOKEN, " ".join(self.lines))
        self.assertFalse(os.path.exists(os.path.join(self.panel_dir, "panel.caddy")))
        self.run.assert_not_called()

    def test_token_without_dns_permission_stops_before_lego(self):
        cf = FakeCF().patch(self)
        with mock.patch.object(cloudflare, "find_records",
                               side_effect=cloudflare.CFError("GET /zones/z1/dns_records: Authentication error")):
            self.assertFalse(self.sync(self.state()))
        state, msg = self.step("certificates")
        self.assertEqual(state, "error")
        self.assertIn("Zone > DNS > Edit", msg)
        self.assertEqual(self.lego_calls(), [])
        self.assertIsNone(cf.tunnel)

    def test_caddy_rejects_changes_nothing(self):
        self.validate.return_value = (False, "bad tls line")
        cf = FakeCF().patch(self)
        self.assertFalse(self.sync(self.state()))
        self.assertIn("bad tls line", self.step("caddy")[1])
        self.assertFalse(os.path.exists(os.path.join(self.panel_dir, "panel.caddy")))
        self.assertIsNone(cf.tunnel)

    def test_reload_failure_restores_old_file(self):
        path = os.path.join(self.panel_dir, "panel.caddy")
        with open(path, "w") as f:
            f.write("OLD\n")
        self.run.side_effect = [(False, "boom"), (True, "")]
        with self.assertRaisesRegex(OSError, "boom"):
            edge.write_panel(edge.render_panel(HOST, ZONE))
        with open(path) as f:
            self.assertEqual(f.read(), "OLD\n")
        self.assertEqual(self.run.call_count, 2)


class JobTest(Base):
    def test_job_reports_steps_and_one_at_a_time(self):
        FakeCF().patch(self)
        self.assertTrue(edge.start("set up", edge.sync, self.state(), TOKEN))
        self.assertTrue(edge.wait())
        st = edge.status()
        self.assertTrue(st["ok"], st)
        self.assertEqual([s["state"] for s in st["steps"]], ["ok"] * 5)
        edge.JOB["running"] = True
        self.assertFalse(edge.start("x", edge.sync, {}, TOKEN))

    def test_request_sync_needs_a_setup_and_queues_behind_a_running_job(self):
        self.assertFalse(edge.request_sync())  # nothing set up
        edge.save(self.state())
        self.assertFalse(edge.request_sync())  # no token
        secrets_store.put("cf_api_token", TOKEN)
        edge.JOB["running"] = True
        self.assertTrue(edge.request_sync())
        self.assertTrue(edge.JOB["again"])


class RenewTest(Base):
    def setUp(self):
        super().setUp()
        self.log = []
        self.now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        with open(os.path.join(self.certs, ZONE + ".crt"), "w") as f:
            f.write("OLD\n")

    def save(self, expires):
        edge.save({"zones": {ZONE: {"id": "z1", "account_id": "acc",
                                    "cert": {"expires": expires, "staging": False}}}})

    def renew(self):
        return edge.renew(now=self.now, log=self.log.append)

    def test_nothing_set_up(self):
        self.assertEqual(self.renew(), 0)
        self.assertIn("no certificates", self.log[0])

    def test_not_due(self):
        self.save("2026-03-01T00:00:00Z")
        self.assertEqual(self.renew(), 0)
        self.assertEqual(self.lego_calls(), [])
        self.run.assert_not_called()

    def test_due_without_token_fails(self):
        self.save("2026-01-10T00:00:00Z")
        self.assertEqual(self.renew(), 1)

    def test_due_renews_and_reloads(self):
        self.save("2026-01-10T00:00:00Z")
        secrets_store.put("cf_api_token", TOKEN)
        self.assertEqual(self.renew(), 0, self.log)
        self.assertEqual(self.lego_calls()[0]["args"][0], "run")
        self.run.assert_called_once_with("reload", "caddy.service")
        with open(os.path.join(self.certs, ZONE + ".crt")) as f:
            self.assertNotEqual(f.read(), "OLD\n")

    def test_failure_exits_nonzero(self):
        self.save("2026-01-10T00:00:00Z")
        secrets_store.put("cf_api_token", TOKEN)
        write_fake_lego(self.lego, self.cert, fail=True)
        self.assertEqual(self.renew(), 1)
        self.assertNotIn(TOKEN, " ".join(self.log))


@unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
class CertInfoTest(Base):
    def test_reads_expiry_and_issuer(self):
        info = edge.cert_info(self.cert)
        self.assertIn(ZONE, info["issuer"])
        self.assertRegex(info["expires"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


if __name__ == "__main__":
    unittest.main()
