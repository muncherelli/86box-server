import io
import json
import unittest
import urllib.error
from unittest import mock

import cloudflare

TOKEN = "tok-SECRET-1234"


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(result, **extra):
    return FakeResp(json.dumps(dict({"success": True, "errors": [], "result": result}, **extra)).encode())


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body, dict(req.header_items()), timeout))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class ClientTest(unittest.TestCase):
    def run_with(self, *responses):
        rec = Recorder(*responses)
        return rec, mock.patch("urllib.request.urlopen", rec)

    def test_list_zones_pages_and_auth(self):
        rec, p = self.run_with(
            ok([{"id": "z1", "name": "a.example", "account": {"id": "acc", "name": "Acct"}}],
               result_info={"page": 1, "total_pages": 2}),
            ok([{"id": "z2", "name": "b.example", "account": {"id": "acc", "name": "Acct"}}],
               result_info={"page": 2, "total_pages": 2}))
        with p:
            zones = cloudflare.list_zones(TOKEN)
        self.assertEqual([z["id"] for z in zones], ["z1", "z2"])
        self.assertEqual(zones[0]["account_id"], "acc")
        method, url, _, headers, timeout = rec.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith("https://api.cloudflare.com/client/v4/zones?"))
        self.assertIn("per_page=50", url)
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(timeout, 20)
        self.assertIn("page=2", rec.calls[1][1])

    def test_create_tunnel_is_remotely_managed(self):
        rec, p = self.run_with(ok({"id": "t1", "name": "n"}))
        with p:
            t = cloudflare.create_tunnel(TOKEN, "acc", "86server-x")
        self.assertEqual(t["id"], "t1")
        method, url, body, _, _ = rec.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.cloudflare.com/client/v4/accounts/acc/cfd_tunnel"))
        self.assertEqual(body, {"name": "86server-x", "config_src": "cloudflare"})

    def test_find_tunnel_filters_by_name(self):
        rec, p = self.run_with(ok([{"id": "old", "name": "other"}, {"id": "t1", "name": "86server-x"}]))
        with p:
            self.assertEqual(cloudflare.find_tunnel(TOKEN, "acc", "86server-x")["id"], "t1")
        self.assertIn("name=86server-x", rec.calls[0][1])
        self.assertIn("is_deleted=false", rec.calls[0][1])

    def test_token_and_config_calls(self):
        rec, p = self.run_with(ok("eyJ-tunnel-token"), ok({"config": {"ingress": []}}), ok({}))
        with p:
            self.assertEqual(cloudflare.tunnel_token(TOKEN, "acc", "t1"), "eyJ-tunnel-token")
            self.assertEqual(cloudflare.get_config(TOKEN, "acc", "t1"), {"ingress": []})
            cloudflare.put_config(TOKEN, "acc", "t1", {"ingress": [cloudflare.CATCH_ALL]})
        self.assertTrue(rec.calls[0][1].endswith("/accounts/acc/cfd_tunnel/t1/token"))
        self.assertEqual(rec.calls[2][0], "PUT")
        self.assertEqual(rec.calls[2][2], {"config": {"ingress": [{"service": "http_status:404"}]}})

    def test_dns_calls(self):
        rec, p = self.run_with(ok([{"id": "r1", "name": "Panel.Example.com", "type": "A"},
                                   {"id": "r2", "name": "x.example.com", "type": "A"}]),
                               ok({"id": "r3"}), ok({"id": "r3"}), ok({}))
        with p:
            found = cloudflare.find_records(TOKEN, "z1", "panel.example.com")
            cloudflare.create_cname(TOKEN, "z1", "panel.example.com", "t1.cfargotunnel.com", "note")
            cloudflare.update_cname(TOKEN, "z1", "r3", "panel.example.com", "t1.cfargotunnel.com")
            cloudflare.delete_record(TOKEN, "z1", "r3")
        self.assertEqual([r["id"] for r in found], ["r1"])
        self.assertIn("name.exact=panel.example.com", rec.calls[0][1])
        self.assertEqual(rec.calls[1][2], {"type": "CNAME", "name": "panel.example.com",
                                           "content": "t1.cfargotunnel.com", "proxied": True, "ttl": 1,
                                           "comment": "note"})
        self.assertEqual((rec.calls[2][0], rec.calls[3][0]), ("PATCH", "DELETE"))
        self.assertTrue(rec.calls[3][1].endswith("/zones/z1/dns_records/r3"))

    def test_errors_carry_cloudflare_messages_not_the_token(self):
        err = urllib.error.HTTPError("u", 403, "Forbidden", {},
                                     io.BytesIO(json.dumps({"success": False, "errors": [
                                         {"code": 9109, "message": "Invalid access token"}]}).encode()))
        rec, p = self.run_with(err)
        with p, self.assertRaises(cloudflare.CFError) as cm:
            cloudflare.list_zones(TOKEN)
        self.assertIn("Invalid access token", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_unsuccessful_200_and_network_error(self):
        rec, p = self.run_with(FakeResp(b'{"success": false, "errors": [{"message": "nope"}]}'),
                               urllib.error.URLError("no route"))
        with p:
            with self.assertRaisesRegex(cloudflare.CFError, "nope"):
                cloudflare.get_tunnel(TOKEN, "acc", "t1")
            with self.assertRaisesRegex(cloudflare.CFError, "cannot reach Cloudflare"):
                cloudflare.get_tunnel(TOKEN, "acc", "t1")


class MergeTest(unittest.TestCase):
    def test_keeps_others_replaces_ours_catch_all_last(self):
        cfg = {"ingress": [{"hostname": "a.example", "service": "http://localhost:1"},
                           {"hostname": "panel.example", "service": "http://old"},
                           {"service": "http_status:404"},
                           {"hostname": "b.example", "service": "http://localhost:2"}],
               "warp-routing": {"enabled": False}}
        out = cloudflare.merge_ingress(cfg, "panel.example", {"service": "https://localhost:443"})
        self.assertEqual([r.get("hostname") for r in out["ingress"]], ["a.example", "b.example", "panel.example", None])
        self.assertEqual(out["ingress"][-1], {"service": "http_status:404"})
        self.assertEqual(out["ingress"][2]["service"], "https://localhost:443")
        self.assertEqual(out["warp-routing"], {"enabled": False})
        self.assertEqual(len(cfg["ingress"]), 4)  # input untouched

    def test_remove_and_empty(self):
        out = cloudflare.merge_ingress({"ingress": [{"hostname": "p", "service": "x"}]}, "p", None)
        self.assertEqual(out["ingress"], [{"service": "http_status:404"}])
        self.assertEqual(cloudflare.merge_ingress({}, "p", {"service": "s"})["ingress"],
                         [{"service": "s", "hostname": "p"}, {"service": "http_status:404"}])


if __name__ == "__main__":
    unittest.main()
