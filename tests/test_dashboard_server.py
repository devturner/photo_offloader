#!/usr/bin/env python3
"""Tests for photo_offloader.dashboard.server: routes, and the protections that keep other web pages out."""

import http.client
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from photo_offloader.dashboard import server as srv


class FakeCore:
    def __init__(self):
        self.calls = []
        self.result = {"ok": True, "message": "done"}

    def state(self):
        return {"headline": {"title": "Ready"}}

    def history(self, limit=15):
        self.calls.append(("history", limit))
        return [{"title": "Import"}]

    def act(self, name, params=None):
        self.calls.append((name, params))
        return self.result


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.web = Path(self.tmp.name)
        (self.web / "index.html").write_text("<!doctype html><title>Photo Offloader</title>")
        self.core = FakeCore()
        self.server = srv.DashboardServer(self.core, self.web, token="secret-token")
        self.server.start()
        self.addCleanup(self.server.stop)
        self.port = self.server.port

    def request(self, method, path, *, headers=None, body=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        merged = {"Host": host or f"127.0.0.1:{self.port}"}
        merged.update(headers or {})
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def api(self, method, path, *, token="secret-token", **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if token is not None:
            headers[srv.TOKEN_HEADER] = token
        return self.request(method, path, headers=headers, **kwargs)

    def post(self, name, payload=None, **kwargs):
        headers = {"Content-Type": "application/json", **kwargs.pop("headers", {})}
        return self.api(
            "POST", f"/api/action/{name}", headers=headers, body=json.dumps(payload or {}), **kwargs
        )


class TestPage(ServerCase):
    def test_serves_the_page_with_hardening_headers(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"Photo Offloader", body)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        csp = response.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("http://", csp.replace("http://127", ""))  # nothing external is allowed

    def test_only_the_dashboard_page_is_served_and_paths_cannot_escape(self):
        for path in ("/index.html", "/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/core.py"):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 404, path)

    def test_the_page_refuses_foreign_host_names(self):
        response, _ = self.request("GET", "/", host="evil.example")
        self.assertEqual(response.status, 403)

    def test_a_missing_page_is_a_clear_error_not_a_crash(self):
        (self.web / "index.html").unlink()
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 500)
        self.assertIn(b"install.sh", body)

    def test_the_token_is_only_ever_in_the_url_fragment(self):
        self.assertIn("/#t=secret-token", self.server.url)
        self.assertNotIn("?", self.server.url)


class TestApiAuthentication(ServerCase):
    def test_state_requires_the_token(self):
        self.assertEqual(self.api("GET", "/api/state", token=None)[0].status, 401)
        self.assertEqual(self.api("GET", "/api/state", token="wrong")[0].status, 401)
        self.assertEqual(self.api("GET", "/api/state", token="secret-toke")[0].status, 401)
        response, body = self.api("GET", "/api/state")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["headline"]["title"], "Ready")

    def test_a_correct_token_is_useless_with_a_foreign_host_header(self):
        # DNS rebinding: a hostile site resolves its own name to 127.0.0.1.
        response, _ = self.api("GET", "/api/state", host="attacker.example:%d" % self.port)
        self.assertEqual(response.status, 403)

    def test_a_cross_origin_request_is_refused_even_with_the_token(self):
        response, _ = self.api("GET", "/api/state", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        response, _ = self.api(
            "GET", "/api/state", headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        self.assertEqual(response.status, 200)

    def test_no_cors_headers_are_ever_granted(self):
        response, _ = self.api("GET", "/api/state")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        response, _ = self.api("OPTIONS", "/api/state")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_history_limit_is_clamped(self):
        self.api("GET", "/api/history?limit=99999")
        self.api("GET", "/api/history?limit=abc")
        self.api("GET", "/api/history?limit=0")
        self.assertEqual(
            [c for c in self.core.calls if c[0] == "history"],
            [("history", 100), ("history", 15), ("history", 1)],
        )

    def test_unknown_routes_and_methods(self):
        self.assertEqual(self.api("GET", "/api/nope")[0].status, 404)
        self.assertEqual(self.api("PUT", "/api/state")[0].status, 405)
        self.assertEqual(self.api("DELETE", "/api/state")[0].status, 405)


class TestActions(ServerCase):
    def test_actions_need_the_token(self):
        response, _ = self.post("pause", token=None)
        self.assertEqual(response.status, 401)
        self.assertEqual(self.core.calls, [])

    def test_a_cross_site_form_post_cannot_trigger_anything(self):
        # A plain HTML form cannot set custom headers or application/json.
        response, _ = self.request(
            "POST",
            "/api/action/service_stop",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://evil.example",
            },
            body="mode=force",
        )
        self.assertIn(response.status, (401, 403))
        self.assertEqual(self.core.calls, [])

    def test_a_correct_token_with_the_wrong_content_type_is_refused(self):
        response, _ = self.api(
            "POST", "/api/action/pause", headers={"Content-Type": "text/plain"}, body="{}"
        )
        self.assertEqual(response.status, 415)
        self.assertEqual(self.core.calls, [])

    def test_runs_the_action_with_its_parameters(self):
        response, body = self.post("service_stop", {"mode": "wait"})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "message": "done"})
        self.assertEqual(self.core.calls, [("service_stop", {"mode": "wait"})])

    def test_a_failed_action_is_a_400_with_its_message(self):
        self.core.result = {"ok": False, "message": "nope"}
        response, body = self.post("usb_backup")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(body)["message"], "nope")

    def test_needing_a_choice_is_not_an_error(self):
        self.core.result = {
            "ok": False,
            "needs_choice": True,
            "message": "A transfer is in progress.",
        }
        response, body = self.post("service_stop")
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(body)["needs_choice"])

    def test_bad_bodies_are_rejected(self):
        headers = {"Content-Type": "application/json"}
        response, _ = self.api("POST", "/api/action/pause", headers=headers, body="{not json")
        self.assertEqual(response.status, 400)
        response, _ = self.api("POST", "/api/action/pause", headers=headers, body="[1, 2]")
        self.assertEqual(response.status, 400)
        response, _ = self.api("POST", "/api/action/pause", headers=headers, body="x" * 20000)
        self.assertEqual(response.status, 413)
        self.assertEqual(self.core.calls, [])

    def test_posting_outside_the_action_namespace_is_404(self):
        response, _ = self.api(
            "POST", "/api/state", headers={"Content-Type": "application/json"}, body="{}"
        )
        self.assertEqual(response.status, 404)


class TestInstanceFile(ServerCase):
    def test_instance_file_is_private_and_lets_a_second_launch_find_the_server(self):
        record = Path(self.tmp.name) / "run" / "dashboard.json"
        srv.write_instance_file(record, self.server)
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        info = srv.find_running_instance(record)
        self.assertEqual((info["port"], info["token"]), (self.port, "secret-token"))
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(info["mode"], "browser")

    def test_a_stale_wrong_or_corrupt_instance_file_is_not_trusted(self):
        record = Path(self.tmp.name) / "dashboard.json"
        self.assertIsNone(srv.find_running_instance(record))  # missing
        record.write_text("not json")
        self.assertIsNone(srv.find_running_instance(record))  # corrupt
        record.write_text(json.dumps({"port": self.port, "token": "wrong"}))
        self.assertIsNone(srv.find_running_instance(record))  # wrong token
        record.write_text(json.dumps({"port": 1, "token": "secret-token"}))
        self.assertIsNone(srv.find_running_instance(record))  # nothing listening


class TestPing(ServerCase):
    def test_ping_reports_how_long_since_a_page_last_polled_without_counting_as_a_poll(self):
        self.api("GET", "/api/state")
        time.sleep(0.3)
        first = json.loads(self.api("GET", "/api/ping")[1])["idle_seconds"]
        time.sleep(0.3)
        second = json.loads(self.api("GET", "/api/ping")[1])["idle_seconds"]
        self.assertGreaterEqual(first, 0.25)
        self.assertGreater(second, first, "pinging must not reset the idle clock")

    def test_find_running_instance_returns_the_idle_time(self):
        record = Path(self.tmp.name) / "dashboard.json"
        srv.write_instance_file(record, self.server)
        self.api("GET", "/api/state")
        info = srv.find_running_instance(record)
        self.assertLess(info["idle_seconds"], 5)


class TestIdleShutdown(unittest.TestCase):
    def test_the_server_exits_when_nobody_is_polling_any_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = srv.DashboardServer(FakeCore(), Path(tmp), token="t", idle_timeout=0.4)
            server.start()
            deadline = time.monotonic() + 5
            while server._thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(server._thread.is_alive(), "server kept running with no clients")
            server.httpd.server_close()

    def test_polling_keeps_the_server_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = srv.DashboardServer(FakeCore(), Path(tmp), token="t", idle_timeout=0.6)
            server.start()
            for _ in range(8):
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
                connection.request(
                    "GET",
                    "/api/state",
                    headers={srv.TOKEN_HEADER: "t", "Host": f"127.0.0.1:{server.port}"},
                )
                connection.getresponse().read()
                connection.close()
                time.sleep(0.15)
            self.assertTrue(server._thread.is_alive())
            server.stop()


if __name__ == "__main__":
    unittest.main()
