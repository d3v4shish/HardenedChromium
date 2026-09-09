#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hardened_adapter_pack import (
    DEFAULT_PACK_DIR,
    AdapterError,
    load_adapter_pack,
    validate_crawl_request,
)
from hardened_scrape_broker import (
    Broker,
    BrokerConfig,
    capture_raw_outputs,
    raw_html_js,
    schema_collect_items_js,
    visible_text_js,
)


class AdapterPackTest(unittest.TestCase):

  def test_offline_view_fixtures_select_and_validate(self) -> None:
    fixtures = json.loads(
        (DEFAULT_PACK_DIR / "fixtures.json").read_text(encoding="utf-8"))
    self.assertEqual(1, fixtures["schemaVersion"])
    pack = load_adapter_pack()
    covered_modes: set[str] = set()
    covered_views: dict[str, set[str]] = {}
    for fixture in fixtures["cases"]:
      adapter = pack.resolve("auto", fixture["url"])
      self.assertIsNotNone(adapter)
      self.assertEqual(fixture["adapter"], adapter.id)
      self.assertIn(fixture["view"], adapter.views)
      mode, _targets = validate_crawl_request(fixture, adapter)
      covered_modes.add(mode)
      covered_views.setdefault(adapter.id, set()).add(fixture["view"])
    self.assertEqual(
        {"current", "scope", "targets", "account"}, covered_modes)
    for adapter in pack.adapters:
      self.assertEqual(set(adapter.views), covered_views[adapter.id])

  def test_default_pack_is_verified_and_selects_supported_domains(self) -> None:
    pack = load_adapter_pack()
    self.assertEqual(
        ["x", "linkedin", "facebook", "reddit", "whatsapp"],
        [adapter.id for adapter in pack.adapters])
    self.assertEqual("x", pack.for_url("https://mobile.x.com/home").id)
    self.assertEqual(
        "linkedin", pack.for_url("https://www.linkedin.com/feed/").id)
    self.assertEqual(
        "whatsapp", pack.for_url("https://web.whatsapp.com/").id)
    self.assertIsNone(pack.for_url("https://notfacebook.com/"))

  def test_schema_checksum_tampering_fails_closed(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      copied = Path(directory) / "pack"
      shutil.copytree(DEFAULT_PACK_DIR, copied)
      with (copied / "x.json").open("a", encoding="utf-8") as stream:
        stream.write("\n")
      with self.assertRaisesRegex(AdapterError, "checksum mismatch"):
        load_adapter_pack(copied)

  def test_overlapping_adapter_domains_fail_closed(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      copied = Path(directory) / "pack"
      shutil.copytree(DEFAULT_PACK_DIR, copied)
      manifest_path = copied / "manifest.json"
      manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
      linkedin = next(
          item for item in manifest["adapters"] if item["id"] == "linkedin")
      linkedin["domains"] = ["mobile.x.com"]
      manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
      with self.assertRaisesRegex(AdapterError, "overlaps"):
        load_adapter_pack(copied)

  def test_rechecks_whatsapp_scope_even_with_updated_checksum(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      copied = Path(directory) / "pack"
      shutil.copytree(DEFAULT_PACK_DIR, copied)
      schema_path = copied / "whatsapp.json"
      schema = json.loads(schema_path.read_text(encoding="utf-8"))
      schema["itemRoot"] = "body"
      encoded = (json.dumps(schema, indent=2) + "\n").encode()
      schema_path.write_bytes(encoded)
      manifest_path = copied / "manifest.json"
      manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
      whatsapp = next(
          item for item in manifest["adapters"] if item["id"] == "whatsapp")
      whatsapp["schemaSha256"] = hashlib.sha256(encoded).hexdigest()
      manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
      with self.assertRaisesRegex(AdapterError, "selected conversation"):
        load_adapter_pack(copied)

  def test_account_requires_confirmation(self) -> None:
    adapter = load_adapter_pack().get("reddit")
    with self.assertRaisesRegex(AdapterError, "confirmAccount=true"):
      validate_crawl_request({"crawlMode": "account"}, adapter)
    mode, targets = validate_crawl_request(
        {"crawlMode": "account", "confirmAccount": True}, adapter)
    self.assertEqual("account", mode)
    self.assertEqual([], targets)

  def test_whatsapp_cannot_switch_or_enumerate_conversations(self) -> None:
    pack = load_adapter_pack()
    adapter = pack.get("whatsapp")
    self.assertEqual(("current", "scope"), adapter.modes)
    self.assertIn("conversation-panel-messages", adapter.snapshot_selector)
    self.assertEqual(
        adapter.schema["scopeRoot"], adapter.snapshot_selector)
    self.assertTrue(adapter.schema["itemRoot"].startswith("#main "))
    for mode in ("targets", "account"):
      request = {"crawlMode": mode, "targets": ["https://web.whatsapp.com/"]}
      request["confirmAccount"] = True
      with self.assertRaisesRegex(AdapterError, "does not support"):
        validate_crawl_request(request, adapter)
    with self.assertRaisesRegex(AdapterError, "cannot bypass"):
      pack.resolve("none", "https://web.whatsapp.com/")

  def test_whatsapp_snapshots_are_root_scoped(self) -> None:
    self.assertIn('document.querySelector(rootSelector)', raw_html_js(1000, "#main"))
    self.assertIn('document.querySelector(rootSelector)', visible_text_js(1000, "#main"))

  def test_whatsapp_incremental_observer_is_conversation_scoped(self) -> None:
    adapter = load_adapter_pack().get("whatsapp")
    schema = dict(adapter.schema)
    schema.update({
        "adapterId": adapter.id,
        "adapterVersion": adapter.version,
    })
    expression = schema_collect_items_js(schema)
    self.assertIn("state.mutation.observe(collectionRoot", expression)
    self.assertIn("scanTree(collectorState, collectorState.collectionRoot)",
                  expression)
    self.assertIn("if (scopeSelector && !scopeRoot)", expression)

  def test_whatsapp_capture_omits_full_document_snapshot(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      job = broker.create_job({"url": "https://web.whatsapp.com/"})
      fake_cdp = mock.Mock()
      responses = [
          {"href": job.url, "title": "fixture", "html": "<main></main>",
           "truncated": False, "length": 13},
          {"href": job.url, "title": "fixture", "text": "rendered message",
           "truncated": False, "length": 16},
      ]
      try:
        with mock.patch(
            "hardened_scrape_broker.evaluate", side_effect=responses) as evaluate:
          capture_raw_outputs(job, fake_cdp, broker.adapter_pack.get("whatsapp"))
        expressions = [call.args[1] for call in evaluate.call_args_list]
        for expression in expressions:
          self.assertIn("conversation-panel-messages", expression)
        fake_cdp.command.assert_not_called()
        self.assertIn("scope forbids", job.exports["snapshotMhtmlOmitted"])
      finally:
        broker.close()

  def test_mhtml_redirect_outside_adapter_domains_is_not_written(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      job = broker.create_job({
          "url": "https://x.com/home",
          "crawlMode": "current",
          "rawSnapshots": True,
      })
      fake_cdp = mock.Mock()
      fake_cdp.command.return_value = {"data": "cross-domain mhtml"}
      responses = [
          {"href": job.url, "title": "X", "html": "<main></main>",
           "truncated": False, "length": 13},
          {"href": "https://example.com/redirected"},
      ]
      try:
        with mock.patch(
            "hardened_scrape_broker.evaluate", side_effect=responses):
          with self.assertRaisesRegex(ValueError, "snapshot navigation left"):
            capture_raw_outputs(job, fake_cdp, broker.adapter_pack.get("x"))
        self.assertNotIn("snapshotMhtml", job.exports)
      finally:
        broker.close()

  def test_job_automatically_records_adapter_contract(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      try:
        job = broker.create_job({
            "url": "https://www.reddit.com/r/test/",
            "crawlMode": "current",
        })
        self.assertEqual("reddit", job.config["adapter_id"])
        self.assertEqual("1.0.0", job.config["adapter_version"])
        self.assertEqual("current", job.config["crawl_mode"])
        self.assertEqual("reddit", job.config["schema"]["adapterId"])
      finally:
        broker.close()

  def test_current_mode_collects_once_without_scrolling(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      job = broker.create_job({
          "url": "https://x.com/home",
          "crawlMode": "current",
          "rawSnapshots": False,
      })
      fake_cdp = mock.Mock()
      fake_cdp.command.return_value = {}
      page = {
          "href": job.url,
          "title": "X",
          "items": [{
              "adapter": "x",
              "adapterVersion": "1.0.0",
              "key": "post-1",
              "text": "rendered post",
          }],
      }
      try:
        with (mock.patch(
            "hardened_scrape_broker.cdp_create_tab",
            return_value={"id": "tab", "webSocketDebuggerUrl": "ws://tab"}),
              mock.patch(
                  "hardened_scrape_broker.CdpWebSocket",
                  return_value=fake_cdp),
              mock.patch("hardened_scrape_broker.wait_for_document_ready"),
              mock.patch(
                  "hardened_scrape_broker.evaluate", return_value=page) as evaluate):
          broker.run_job(job)
        self.assertEqual("completed", job.status)
        self.assertEqual("current_rendered_view_collected", job.reason)
        self.assertEqual(1, len(job.items))
        self.assertEqual("x", job.items[0]["adapter"])
        self.assertEqual("1.0.0", job.items[0]["adapter_version"])
        self.assertEqual(
            "hardened-default-adapters", job.items[0]["adapter_pack_id"])
        self.assertEqual("1.0.0", job.items[0]["adapter_pack_version"])
        self.assertEqual(1, evaluate.call_count)
        events, _ = broker.event_hub.events_after(job.id, 0)
        self.assertIn("adapter_progress", [event["type"] for event in events])
      finally:
        broker.close()

  def test_targets_mode_navigates_each_same_site_fixture(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      targets = [
          "https://www.reddit.com/r/test/comments/one/thread/",
          "https://www.reddit.com/r/test/comments/two/thread/",
      ]
      job = broker.create_job({
          "url": "https://www.reddit.com/r/test/",
          "crawlMode": "targets",
          "targets": targets,
          "noProgressLimit": 1,
          "rawSnapshots": False,
      })
      fake_cdp = mock.Mock()
      fake_cdp.command.return_value = {}
      try:
        with (mock.patch(
            "hardened_scrape_broker.cdp_create_tab",
            return_value={"id": "tab", "webSocketDebuggerUrl": "ws://tab"}),
              mock.patch(
                  "hardened_scrape_broker.CdpWebSocket",
                  return_value=fake_cdp),
              mock.patch("hardened_scrape_broker.wait_for_document_ready"),
              mock.patch("hardened_scrape_broker.evaluate", return_value={
                  "href": targets[0], "title": "fixture", "items": []})):
          broker.run_job(job)
        navigations = [
            call.args[1]["url"] for call in fake_cdp.command.call_args_list
            if call.args and call.args[0] == "Page.navigate"]
        self.assertEqual(targets, navigations)
        self.assertEqual("crawl_scope_exhausted", job.reason)
      finally:
        broker.close()

  def test_navigation_redirect_outside_adapter_domains_fails_closed(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      job = broker.create_job({
          "url": "https://x.com/home",
          "crawlMode": "current",
          "rawSnapshots": False,
      })
      fake_cdp = mock.Mock()
      fake_cdp.command.return_value = {}
      try:
        with (mock.patch(
            "hardened_scrape_broker.cdp_create_tab",
            return_value={"id": "tab", "webSocketDebuggerUrl": "ws://tab"}),
              mock.patch(
                  "hardened_scrape_broker.CdpWebSocket",
                  return_value=fake_cdp),
              mock.patch("hardened_scrape_broker.wait_for_document_ready"),
              mock.patch(
                  "hardened_scrape_broker.evaluate", return_value={
                      "href": "https://example.com/redirected",
                      "title": "outside",
                      "items": [],
                  })):
          broker.run_job(job)
        self.assertEqual("failed", job.status)
        self.assertIn("navigation left the x adapter domains", job.error)
        self.assertEqual([], job.items)
      finally:
        broker.close()

  def test_snapshot_redirect_outside_adapter_domains_fails_closed(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      job = broker.create_job({
          "url": "https://www.reddit.com/r/test/",
          "crawlMode": "current",
          "rawSnapshots": True,
      })
      fake_cdp = mock.Mock()
      fake_cdp.command.return_value = {}
      collected = {
          "href": job.url,
          "title": "Reddit",
          "items": [],
      }
      redirected_snapshot = {
          "href": "https://example.com/redirected",
          "title": "outside",
          "html": "<p>must not be saved</p>",
          "truncated": False,
          "length": 24,
      }
      try:
        with (mock.patch(
            "hardened_scrape_broker.cdp_create_tab",
            return_value={"id": "tab", "webSocketDebuggerUrl": "ws://tab"}),
              mock.patch(
                  "hardened_scrape_broker.CdpWebSocket",
                  return_value=fake_cdp),
              mock.patch("hardened_scrape_broker.wait_for_document_ready"),
              mock.patch(
                  "hardened_scrape_broker.evaluate",
                  side_effect=[collected, redirected_snapshot])):
          broker.run_job(job)
        self.assertEqual("failed", job.status)
        self.assertIn(
            "snapshot navigation left the reddit adapter domains", job.error)
        self.assertNotIn("rawHtml", job.exports)
      finally:
        broker.close()

  def test_scope_mode_advances_until_fixture_is_exhausted(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      job = broker.create_job({
          "url": "https://www.linkedin.com/feed/",
          "crawlMode": "scope",
          "noProgressLimit": 1,
          "scrollDelayMs": 250,
          "rawSnapshots": False,
      })
      fake_cdp = mock.Mock()
      fake_cdp.command.return_value = {}
      collection_count = 0
      advance_count = 0

      def fixture_evaluate(_cdp: object, expression: str,
                           timeout: int = 0) -> dict[str, object]:
        nonlocal collection_count, advance_count
        del timeout
        if "const direction =" in expression:
          advance_count += 1
          return {"before": 0, "after": 500}
        collection_count += 1
        items = ([{"key": "update-1", "text": "rendered update"}]
                 if collection_count == 1 else [])
        return {"href": job.url, "title": "fixture", "items": items}

      try:
        with (mock.patch(
            "hardened_scrape_broker.cdp_create_tab",
            return_value={"id": "tab", "webSocketDebuggerUrl": "ws://tab"}),
              mock.patch(
                  "hardened_scrape_broker.CdpWebSocket",
                  return_value=fake_cdp),
              mock.patch("hardened_scrape_broker.wait_for_document_ready"),
              mock.patch("hardened_scrape_broker.sleep_interruptibly"),
              mock.patch(
                  "hardened_scrape_broker.evaluate",
                  side_effect=fixture_evaluate)):
          broker.run_job(job)
        self.assertEqual(2, collection_count)
        self.assertEqual(1, advance_count)
        self.assertEqual("crawl_scope_exhausted", job.reason)
      finally:
        broker.close()

  def test_confirmed_account_mode_discovers_rendered_fixture_links(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      discovered = "https://x.com/example/status/123"
      job = broker.create_job({
          "url": "https://x.com/example",
          "crawlMode": "account",
          "confirmAccount": True,
          "noProgressLimit": 1,
          "rawSnapshots": False,
      })
      fake_cdp = mock.Mock()
      fake_cdp.command.return_value = {}

      def fixture_evaluate(_cdp: object, expression: str,
                           timeout: int = 0) -> dict[str, object]:
        del timeout
        if "const urls = [];" in expression:
          return {"urls": [
              discovered,
              "https://user:secret@x.com/example/status/2",
              "https://example.com/outside",
          ]}
        return {"href": job.url, "title": "fixture", "items": []}

      try:
        with (mock.patch(
            "hardened_scrape_broker.cdp_create_tab",
            return_value={"id": "tab", "webSocketDebuggerUrl": "ws://tab"}),
              mock.patch(
                  "hardened_scrape_broker.CdpWebSocket",
                  return_value=fake_cdp),
              mock.patch("hardened_scrape_broker.wait_for_document_ready"),
              mock.patch(
                  "hardened_scrape_broker.evaluate",
                  side_effect=fixture_evaluate)):
          broker.run_job(job)
        navigations = [
            call.args[1]["url"] for call in fake_cdp.command.call_args_list
            if call.args and call.args[0] == "Page.navigate"]
        self.assertEqual([job.url, discovered], navigations)
        events, _ = broker.event_hub.events_after(job.id, 0)
        self.assertIn(
            "adapter_targets_discovered",
            [event["type"] for event in events])
      finally:
        broker.close()

  def test_adapter_rejects_cross_site_targets(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      try:
        with self.assertRaisesRegex(ValueError, "outside the reddit"):
          broker.create_job({
              "url": "https://www.reddit.com/r/test/",
              "crawlMode": "targets",
              "targets": ["https://example.com/"],
          })
      finally:
        broker.close()

  def test_whatsapp_rejects_custom_schema(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      try:
        with self.assertRaisesRegex(ValueError, "default schema"):
          broker.create_job({
              "url": "https://web.whatsapp.com/",
              "schema": {
                  "name": "unsafe",
                  "itemRoot": "body",
                  "fields": [{"name": "text", "selector": ""}],
              },
          })
      finally:
        broker.close()


if __name__ == "__main__":
  unittest.main()
