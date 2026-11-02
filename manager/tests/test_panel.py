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
import panel
import secrets_store

HOST = "panel.example.com"
TOKEN = "cf-token-abcd1234"


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
open(os.path.join(d, name + ".issuer.crt"), "w").write("ISSUER\\n")
""" % (fail, cert_src, key_text))
    os.chmod(path, 0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "manager"))
        self.certs = os.path.join(self.root, "certs")
        self.panel_dir = os.path.join(self.root, "panel.d")
        os.makedirs(self.certs)
        os.makedirs(self.panel_dir)
        self.lego = os.path.join(self.root, "bin", "lego")
        os.makedirs(os.path.dirname(self.lego))
        self.saved = dict(config.CFG)
        config.CFG.update(data_root=self.root, certs_dir=self.certs, panel_dir=self.panel_dir, lego_bin=self.lego)
        self.cert = os.path.join(self.root, "cert.pem")
        if shutil.which("openssl"):
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                            "-nodes", "-days", "60", "-subj", "/CN=" + HOST, "-keyout", os.path.join(self.root, "k.pem"),
                            "-out", self.cert], check=True, capture_output=True)
        else:
            with open(self.cert, "w") as f:
                f.write("CERT\n")
        write_fake_lego(self.lego, self.cert)
        self.units = mock.patch("units.run", return_value=(True, ""))
        self.run = self.units.start()
        self.valid = mock.patch("sites.caddy_validate", return_value=(True, "valid"))
        self.validate = self.valid.start()
        panel.JOB.update(kind="", running=False, steps=[], done="", ok=None)

    def tearDown(self):
        mock.patch.stopall()
        config.CFG.clear()
        config.CFG.update(self.saved)
        self.tmp.cleanup()

    def lego_calls(self):
        with open(os.path.join(os.path.dirname(self.lego), "lego-calls.json")) as f:
            return json.load(f)

    def state(self, **kw):
        return dict({"hostname": HOST, "zone_id": "z1", "zone_name": "example.com", "account_id": "acc",
                     "email": "", "staging": True, "tunnel": True}, **kw)

    def steps(self):
        return {s["name"]: (s["state"], s["msg"]) for s in panel.status()["steps"]}

    def run_job(self, fn, steps, *args):
        self.assertTrue(panel.start("job", fn, steps, *args))
        self.assertTrue(panel.wait())
        return panel.status()


class FakeCF:
    """In-memory Cloudflare for the job steps."""
    def __init__(self, records=None, tunnel=None):
        self.records = list(records or [])
        self.tunnel = tunnel
        self.config = {"ingress": [{"hostname": "other.example.com", "service": "http://localhost:9"},
                                   {"service": "http_status:404"}]}
        self.deleted = []
        self.calls = []

    def patch(self, test):
        for name in ("find_tunnel", "create_tunnel", "get_config", "put_config", "tunnel_token", "find_records",
                     "create_cname", "update_cname", "delete_record", "clean_connections", "delete_tunnel"):
            p = mock.patch.object(cloudflare, name, getattr(self, name))
            p.start()
            test.addCleanup(p.stop)

    def find_tunnel(self, token, acct, name):
        return self.tunnel

    def create_tunnel(self, token, acct, name):
        self.tunnel = {"id": "tun-1", "name": name}
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
        self.records.append({"id": "new", "name": name, "type": "CNAME", "content": target})

    def update_cname(self, token, zone, rid, name, target, comment=""):
        self.calls.append(("update", rid))

    def delete_record(self, token, zone, rid):
        self.deleted.append(rid)
        self.records = [r for r in self.records if r["id"] != rid]

    def clean_connections(self, token, acct, tid):
        self.calls.append(("clean", tid))

    def delete_tunnel(self, token, acct, tid):
        self.calls.append(("delete", tid))


class NamesTest(Base):
    def test_hostname_for(self):
        self.assertEqual(panel.hostname_for("Box", "example.com"), "box.example.com")
        self.assertEqual(panel.hostname_for("@", "example.com"), "example.com")
        self.assertEqual(panel.hostname_for("a.b", "example.com"), "a.b.example.com")
        for bad in ("-x", "a_b", "a..b", "x y", "a\nb"):
            with self.assertRaises(ValueError):
                panel.hostname_for(bad, "example.com")

    def test_render_caddy(self):
        self.assertEqual(panel.render_caddy(HOST),
                         "# managed by 86server: do not edit\n%s {\n\ttls %s/%s.crt %s/%s.key\n"
                         "\timport b86_manager {http.request.scheme}\n}\n" % (HOST, self.certs, HOST, self.certs, HOST))
        with self.assertRaises(ValueError):
            panel.render_caddy("bad host {")

    def test_tunnel_name(self):
        self.assertEqual(panel.tunnel_name(HOST), "86server-panel-example-com")


class SetupTest(Base):
    def test_full_setup(self):
        cf = FakeCF()
        cf.patch(self)
        st = self.run_job(panel.setup, panel.STEPS, self.state(), TOKEN, False)
        self.assertTrue(st["ok"], st)
        self.assertEqual({k: v[0] for k, v in self.steps().items()}, {s: "ok" for s in panel.STEPS})
        # lego: v5 'run' with DNS-01 via Cloudflare, staging, token only in the environment
        call = self.lego_calls()[0]
        self.assertEqual(call["args"][0], "run")
        for flag in ("--accept-tos", "--dns", "cloudflare", "--domains", HOST, "--server", "letsencrypt-staging"):
            self.assertIn(flag, call["args"])
        self.assertNotIn("--email", call["args"])
        self.assertEqual(call["token"], TOKEN)
        self.assertNotIn(TOKEN, " ".join(call["args"]))
        # certificate copies for caddy
        for ext in (".crt", ".key"):
            path = os.path.join(self.certs, HOST + ext)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        # caddy site, validated before it was written, then reloaded
        with open(os.path.join(self.panel_dir, "panel.caddy")) as f:
            self.assertEqual(f.read(), panel.render_caddy(HOST))
        self.validate.assert_called_once_with(panel.render_caddy(HOST))
        self.assertIn(mock.call("reload", "caddy.service"), self.run.call_args_list)
        # tunnel route, other routes kept, catch-all last
        hosts = [r.get("hostname") for r in cf.config["ingress"]]
        self.assertEqual(hosts, ["other.example.com", HOST, None])
        ours = cf.config["ingress"][1]
        self.assertEqual(ours["service"], "https://localhost:443")
        self.assertEqual(ours["originRequest"], {"originServerName": HOST, "httpHostHeader": HOST})
        env = os.path.join(self.root, "manager", "cloudflared.env")
        with open(env) as f:
            self.assertEqual(f.read(), "TUNNEL_TOKEN=TUNNEL-SECRET\n")
        self.assertEqual(stat.S_IMODE(os.stat(env).st_mode), 0o600)
        self.assertEqual(cf.records[0]["content"], "tun-1.cfargotunnel.com")
        self.run.assert_any_call("restart", "86server-cloudflared.service")
        s = panel.load()
        self.assertTrue(s["active"])
        self.assertEqual(s["tunnel_id"], "tun-1")
        self.assertIn(HOST, config.manager_hosts())
        if shutil.which("openssl"):
            self.assertGreater(panel.days_left(s), 58)

    def test_existing_record_is_not_overwritten(self):
        cf = FakeCF(records=[{"id": "a1", "name": HOST, "type": "A", "content": "192.0.2.1"}])
        cf.patch(self)
        st = self.run_job(panel.setup, panel.STEPS, self.state(), TOKEN, False)
        self.assertFalse(st["ok"])
        state, msg = self.steps()["dns"]
        self.assertEqual(state, "error")
        self.assertIn("A 192.0.2.1", msg)
        self.assertEqual(cf.deleted, [])
        self.assertEqual(self.steps()["cloudflared"][0], "pending")

    def test_replace_existing_record_when_asked(self):
        cf = FakeCF(records=[{"id": "a1", "name": HOST, "type": "A", "content": "192.0.2.1"}])
        cf.patch(self)
        st = self.run_job(panel.setup, panel.STEPS, self.state(), TOKEN, True)
        self.assertTrue(st["ok"])
        self.assertEqual(cf.deleted, ["a1"])

    def test_our_cname_is_updated_not_duplicated(self):
        cf = FakeCF(records=[{"id": "c1", "name": HOST, "type": "CNAME", "content": "tun-9.cfargotunnel.com"}],
                    tunnel={"id": "tun-9", "name": panel.tunnel_name(HOST)})
        cf.patch(self)
        self.assertTrue(self.run_job(panel.setup, panel.STEPS, self.state(), TOKEN, False)["ok"])
        self.assertEqual(cf.calls, [("update", "c1")])
        self.assertEqual(len(cf.records), 1)

    def test_no_tunnel_skips_cloudflare_steps(self):
        cf = FakeCF()
        cf.patch(self)
        st = self.run_job(panel.setup, panel.STEPS, self.state(tunnel=False, staging=False, email="a@example.org"),
                          TOKEN, False)
        self.assertTrue(st["ok"])
        self.assertEqual(self.steps()["tunnel"][0], "skipped")
        args = self.lego_calls()[0]["args"]
        self.assertIn("letsencrypt", args)
        self.assertEqual(args[args.index("--email") + 1], "a@example.org")
        self.assertIsNone(cf.tunnel)

    def test_lego_failure_stops_and_hides_token(self):
        write_fake_lego(self.lego, self.cert, fail=True)
        st = self.run_job(panel.setup, panel.STEPS, self.state(), TOKEN, False)
        self.assertFalse(st["ok"])
        state, msg = self.steps()["certificate"]
        self.assertEqual(state, "error")
        self.assertIn("some failure", msg)
        self.assertNotIn(TOKEN, msg)
        self.assertFalse(os.path.exists(os.path.join(self.panel_dir, "panel.caddy")))
        self.run.assert_not_called()

    def test_caddy_rejects_changes_nothing(self):
        self.validate.return_value = (False, "bad tls line")
        cf = FakeCF()
        cf.patch(self)
        st = self.run_job(panel.setup, panel.STEPS, self.state(), TOKEN, False)
        self.assertFalse(st["ok"])
        self.assertIn("bad tls line", self.steps()["caddy"][1])
        self.assertFalse(os.path.exists(os.path.join(self.panel_dir, "panel.caddy")))
        self.assertIsNone(cf.tunnel)
        self.assertFalse(panel.load()["active"])

    def test_reload_failure_restores_old_file(self):
        path = os.path.join(self.panel_dir, "panel.caddy")
        with open(path, "w") as f:
            f.write("OLD\n")
        self.run.side_effect = [(False, "boom"), (True, "")]
        with self.assertRaisesRegex(OSError, "boom"):
            panel.write_caddy(panel.render_caddy(HOST))
        with open(path) as f:
            self.assertEqual(f.read(), "OLD\n")
        self.assertEqual(self.run.call_count, 2)

    def test_one_job_at_a_time(self):
        panel.JOB["running"] = True
        self.assertFalse(panel.start("x", lambda: True, ["a"]))


class RemoveTest(Base):
    def test_remove_undoes_ours_only(self):
        cf = FakeCF(records=[{"id": "c1", "name": HOST, "type": "CNAME", "content": "tun-1.cfargotunnel.com"},
                             {"id": "x1", "name": HOST, "type": "TXT", "content": "keep me"}])
        cf.config["ingress"].insert(0, {"hostname": HOST, "service": "https://localhost:443"})
        cf.patch(self)
        state = self.state(active=True, tunnel_id="tun-1")
        panel.save(state)
        with open(os.path.join(self.panel_dir, "panel.caddy"), "w") as f:
            f.write(panel.render_caddy(HOST))
        env = os.path.join(self.root, "manager", "cloudflared.env")
        with open(env, "w") as f:
            f.write("TUNNEL_TOKEN=x\n")
        st = self.run_job(panel.remove, panel.REMOVE_STEPS, state, TOKEN, False)
        self.assertTrue(st["ok"], st)
        self.assertEqual([r.get("hostname") for r in cf.config["ingress"]], ["other.example.com", None])
        self.assertEqual(cf.deleted, ["c1"])
        self.assertFalse(os.path.exists(env))
        self.assertFalse(os.path.exists(os.path.join(self.panel_dir, "panel.caddy")))
        self.run.assert_any_call("stop", "86server-cloudflared.service")
        self.assertEqual(self.steps()["delete tunnel"][0], "skipped")
        self.assertFalse(panel.load()["active"])
        self.assertNotIn(HOST, config.manager_hosts())

    def test_remove_can_delete_the_tunnel(self):
        cf = FakeCF()
        cf.patch(self)
        state = self.state(active=True, tunnel_id="tun-1")
        st = self.run_job(panel.remove, panel.REMOVE_STEPS, state, TOKEN, True)
        self.assertTrue(st["ok"], st)
        self.assertEqual(cf.calls, [("clean", "tun-1"), ("delete", "tun-1")])
        self.assertNotIn("tunnel_id", panel.load())


class RenewTest(Base):
    def setUp(self):
        super().setUp()
        self.log = []
        self.now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    def renew(self):
        return panel.renew(now=self.now, log=self.log.append)

    def test_nothing_set_up(self):
        self.assertEqual(self.renew(), 0)
        self.assertIn("no panel certificate", self.log[0])

    def test_not_due(self):
        panel.save(self.state(active=True, cert={"expires": "2026-03-01T00:00:00Z"}))
        self.assertEqual(self.renew(), 0)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.lego), "lego-calls.json")))
        self.run.assert_not_called()

    def test_due_without_token_fails(self):
        panel.save(self.state(active=True, cert={"expires": "2026-01-10T00:00:00Z"}))
        self.assertEqual(self.renew(), 1)

    def test_due_renews_and_reloads(self):
        panel.save(self.state(active=True, cert={"expires": "2026-01-10T00:00:00Z"}))
        secrets_store.put("cf_api_token", TOKEN)
        self.assertEqual(self.renew(), 0, self.log)
        args = self.lego_calls()[0]["args"]
        self.assertEqual(args[0], "run")
        self.assertEqual(args[args.index("--renew-days") + 1], "30")
        self.run.assert_called_once_with("reload", "caddy.service")
        self.assertTrue(os.path.exists(os.path.join(self.certs, HOST + ".crt")))

    def test_failure_exits_nonzero(self):
        panel.save(self.state(active=True, cert={"expires": "2026-01-10T00:00:00Z"}))
        secrets_store.put("cf_api_token", TOKEN)
        write_fake_lego(self.lego, self.cert, fail=True)
        self.assertEqual(self.renew(), 1)
        self.assertNotIn(TOKEN, " ".join(self.log))
        self.run.assert_not_called()


@unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
class CertInfoTest(Base):
    def test_reads_expiry_and_issuer(self):
        info = panel.cert_info(self.cert)
        self.assertIn(HOST, info["issuer"])
        self.assertRegex(info["expires"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


if __name__ == "__main__":
    unittest.main()
