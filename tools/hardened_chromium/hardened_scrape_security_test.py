#!/usr/bin/env python3
"""Focused security regressions for loopback no-auth request validation."""

import http.client
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from hardened_scrape_broker import (
    Broker,
    BrokerConfig,
    BrokerRequestHandler,
    loopback_host_header_allowed,
    loopback_origin_allowed,
)


class NoAuthRequestSecurityTest(unittest.TestCase):

  def test_native_loopback_hosts_are_allowed(self) -> None:
    self.assertTrue(loopback_host_header_allowed("127.0.0.1:8877", 8877))
    self.assertTrue(loopback_host_header_allowed("localhost:8877", 8877))
    self.assertTrue(loopback_host_header_allowed("[::1]:8877", 8877))

  def test_rebinding_and_wrong_ports_are_rejected(self) -> None:
    self.assertFalse(loopback_host_header_allowed("evil.example:8877", 8877))
    self.assertFalse(loopback_host_header_allowed("127.0.0.1:9999", 8877))
    self.assertFalse(loopback_host_header_allowed("", 8877))

  def test_native_clients_and_same_origin_ui_are_allowed(self) -> None:
    self.assertTrue(loopback_origin_allowed("", 8877))
    self.assertTrue(loopback_origin_allowed("http://127.0.0.1:8877", 8877))
    self.assertTrue(loopback_origin_allowed("http://localhost:8877", 8877))

  def test_foreign_and_opaque_origins_are_rejected(self) -> None:
    self.assertFalse(loopback_origin_allowed("https://evil.example", 8877))
    self.assertFalse(loopback_origin_allowed("null", 8877))
    self.assertFalse(loopback_origin_allowed("http://127.0.0.1:9999", 8877))


class NoAuthRequestHandlerTest(unittest.TestCase):

  def setUp(self) -> None:
    self.temporary = tempfile.TemporaryDirectory()
    broker = Broker(BrokerConfig(
        cdp_endpoint="http://127.0.0.1:9222",
        output_root=Path(self.temporary.name),
        token="",
        no_auth=True,
    ))
    try:
      self.server = ThreadingHTTPServer(
          ("127.0.0.1", 0), BrokerRequestHandler)
    except (OSError, PermissionError):
      self.temporary.cleanup()
      self.skipTest("loopback sockets are disabled in this sandbox")
    self.server.broker = broker
    self.port = int(self.server.server_address[1])
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    self.thread.start()

  def tearDown(self) -> None:
    self.server.shutdown()
    self.server.server_close()
    self.thread.join(timeout=2)
    self.temporary.cleanup()

  def request(self, method: str, path: str, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, body

  def test_native_health_request_is_allowed_without_cors(self) -> None:
    status, headers, _body = self.request("GET", "/health")
    self.assertEqual(status, 200)
    self.assertNotIn("Access-Control-Allow-Origin", headers)

  def test_adapters_endpoint_reports_verified_local_pack(self) -> None:
    status, _headers, body = self.request("GET", "/adapters")
    self.assertEqual(status, 200)
    payload = json.loads(body)
    self.assertTrue(payload["pack"]["local"])
    self.assertTrue(payload["pack"]["verified"])
    self.assertEqual(
        ["x", "linkedin", "facebook", "reddit", "whatsapp"],
        [adapter["id"] for adapter in payload["pack"]["adapters"]])

  def test_invalid_adapter_request_returns_stable_bad_request(self) -> None:
    payload = json.dumps({
        "url": "https://www.reddit.com/r/test/",
        "adapter": "not-installed",
    }).encode("utf-8")
    status, _headers, body = self.request(
        "POST", "/jobs", {"Content-Type": "application/json"}, payload)
    self.assertEqual(status, 400)
    response = json.loads(body)
    self.assertFalse(response["ok"])
    self.assertEqual("unknown adapter: not-installed", response["error"])

  def test_foreign_origin_is_rejected_without_cors(self) -> None:
    status, headers, body = self.request(
        "GET", "/health", {"Origin": "https://evil.example"})
    self.assertEqual(status, 403)
    self.assertNotIn("Access-Control-Allow-Origin", headers)
    self.assertIn(b"forbidden_request_origin", body)

  def test_same_origin_preflight_echoes_exact_origin(self) -> None:
    origin = f"http://127.0.0.1:{self.port}"
    status, headers, _body = self.request("OPTIONS", "/jobs", {"Origin": origin})
    self.assertEqual(status, 204)
    self.assertEqual(headers.get("Access-Control-Allow-Origin"), origin)

  def test_dns_rebinding_host_is_rejected(self) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
    connection.putrequest("GET", "/health", skip_host=True)
    connection.putheader("Host", f"evil.example:{self.port}")
    connection.endheaders()
    response = connection.getresponse()
    self.assertEqual(response.status, 403)
    response.read()
    connection.close()

  def test_privacy_rule_round_trip_preserves_independent_sources(self) -> None:
    payload = json.dumps({
        "origin": "https://Example.test/path",
        "cameraSource": "real",
        "microphoneSource": "fake",
        "locationSource": "real",
    }).encode("utf-8")
    status, _headers, body = self.request(
        "POST", "/service/privacy-rules",
        {"Content-Type": "application/json"}, payload)
    self.assertEqual(status, 200)
    saved = json.loads(body)["rule"]
    self.assertEqual(saved["origin"], "https://example.test")
    self.assertEqual(saved["cameraSource"], "real")
    self.assertEqual(saved["microphoneSource"], "fake")
    self.assertEqual(saved["locationSource"], "real")

    status, _headers, body = self.request(
        "GET", "/service/privacy-rules")
    self.assertEqual(status, 200)
    self.assertEqual(len(json.loads(body)["rules"]), 1)

    status, _headers, _body = self.request(
        "DELETE", f"/service/privacy-rules/{saved['id']}")
    self.assertEqual(status, 200)


if __name__ == "__main__":
  unittest.main()
