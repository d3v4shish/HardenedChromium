#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Correctness and isolation tests for broker app event streaming."""

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from hardened_scrape_broker import (
    Broker,
    BrokerAuth,
    BrokerConfig,
    BrokerRequestHandler,
    Job,
    STREAM_QUEUE_MAX_MESSAGES,
)
from hardened_scrape_client import (
    BrokerClient,
    BrokerClientError,
    BrokerEventWebSocket,
)


class StreamIntegrationTest(unittest.TestCase):

  def setUp(self) -> None:
    self.temporary = tempfile.TemporaryDirectory()
    self.broker = Broker(BrokerConfig(
        cdp_endpoint="http://127.0.0.1:1",
        output_root=Path(self.temporary.name),
        token="admin-test-token",
        start_scheduler=False,
    ))
    try:
      self.server = ThreadingHTTPServer(
          ("127.0.0.1", 0), BrokerRequestHandler)
    except (OSError, PermissionError):
      self.broker.close()
      self.temporary.cleanup()
      self.skipTest("loopback sockets are disabled in this sandbox")
    self.server.broker = self.broker
    self.port = int(self.server.server_address[1])
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    self.thread.start()

    self.app_a = self.broker.register_app({
        "name": "Stream app A", "app_id": "stream-a"})
    self.app_b = self.broker.register_app({
        "name": "Stream app B", "app_id": "stream-b"})
    self.job_a = self.add_job("job-a", self.app_a.id)
    self.job_b = self.add_job("job-b", self.app_b.id)
    self.client_a = BrokerClient(
        f"http://127.0.0.1:{self.port}",
        app_id=self.app_a.id,
        app_secret=self.app_a.secret)

  def tearDown(self) -> None:
    if hasattr(self, "server"):
      self.server.shutdown()
      self.server.server_close()
      self.thread.join(timeout=2)
    if hasattr(self, "broker"):
      self.broker.close()
    self.temporary.cleanup()

  def add_job(self, job_id: str, app_id: str) -> Job:
    job = Job(
        id=job_id,
        app_id=app_id,
        url="https://example.test/",
        output_dir=self.broker.config.output_root / app_id / job_id,
        config={},
        status="running")
    with self.broker.jobs_lock:
      self.broker.jobs[job.id] = job
    return job

  def receive_type(
      self, websocket: BrokerEventWebSocket, expected: str,
      timeout: float = 2,
  ) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      message = websocket.receive_json(max(0.01, deadline - time.monotonic()))
      if message and message.get("type") == expected:
        return message
    self.fail(f"did not receive WebSocket message type {expected!r}")

  def test_app_stream_is_multiplexed_ordered_and_isolated(self) -> None:
    websocket = self.client_a.open_event_stream({
        self.job_a.id: 0,
        # This other app's job must be explicitly rejected.
        self.job_b.id: 0,
    })
    self.addCleanup(websocket.close)
    self.receive_type(websocket, "ready")
    subscribed = self.receive_type(websocket, "subscribed")
    self.assertEqual([self.job_a.id], [
        value["jobId"] for value in subscribed["accepted"]])
    self.assertEqual("forbidden", subscribed["rejected"][0]["reason"])

    for value in range(1, 6):
      self.broker.add_event(self.job_a, "sample", {"value": value})
      self.broker.add_event(self.job_b, "private", {"value": value})

    received = [self.receive_type(websocket, "sample") for _ in range(5)]
    self.assertEqual([1, 2, 3, 4, 5], [
        message["sequence"] for message in received])
    self.assertEqual([1, 2, 3, 4, 5], [
        message["data"]["value"] for message in received])
    self.assertIsNone(websocket.receive_json(0.05))

  def test_ticket_is_short_lived_scope_preserving_and_single_use(self) -> None:
    ticket = self.client_a.create_stream_ticket()
    websocket = BrokerEventWebSocket(ticket["webSocketUrl"])
    websocket.connect()
    self.addCleanup(websocket.close)
    self.receive_type(websocket, "ready")

    duplicate = BrokerEventWebSocket(ticket["webSocketUrl"])
    with self.assertRaisesRegex(BrokerClientError, "401"):
      duplicate.connect()

  def test_reconnect_replays_only_events_after_cursor(self) -> None:
    for value in range(8):
      self.broker.add_event(self.job_a, "sample", {"value": value})
    websocket = self.client_a.open_event_stream({self.job_a.id: 5})
    self.addCleanup(websocket.close)
    self.receive_type(websocket, "ready")
    self.receive_type(websocket, "subscribed")
    replay = [self.receive_type(websocket, "sample") for _ in range(3)]
    self.assertEqual([6, 7, 8], [event["sequence"] for event in replay])

  def test_sse_fallback_is_event_driven_and_sequence_resumable(self) -> None:
    def publish() -> None:
      time.sleep(0.05)
      self.broker.add_event(self.job_a, "sample", {"value": 42})
      self.broker.set_job_status(self.job_a, "completed", "test_complete")

    publisher = threading.Thread(target=publish, daemon=True)
    publisher.start()
    started = time.monotonic()
    stream = self.client_a.stream_events(
        self.job_a.id, after_sequence=0)
    event, payload = next(stream)
    elapsed = time.monotonic() - started
    self.assertEqual("sample", event)
    self.assertEqual(42, payload["data"]["value"])
    self.assertEqual(1, payload["sequence"])
    self.assertLess(elapsed, 0.5)
    stream.close()
    publisher.join(timeout=2)


class StreamHubCorrectnessTest(unittest.TestCase):

  def test_slow_consumer_is_closed_instead_of_growing_without_bound(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory), token="test", start_scheduler=False))
      try:
        job = Job(
            id="bounded", app_id="app-a", url="https://example.test/",
            output_dir=Path(directory) / "bounded", config={})
        subscription = broker.event_hub.subscribe(BrokerAuth("app", "app-a"))
        broker.event_hub.add_job(subscription, job, 0)
        for value in range(STREAM_QUEUE_MAX_MESSAGES + 1):
          broker.event_hub.publish(job, "sample", {"value": value})
        self.assertTrue(subscription.closed)
        self.assertEqual("slow_consumer", subscription.close_reason)
        self.assertLessEqual(
            len(subscription.messages), STREAM_QUEUE_MAX_MESSAGES)
      finally:
        broker.close()

  def test_stream_queue_reuses_the_preencoded_event_payload(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory), token="test", start_scheduler=False))
      job = Job(
          id="encoded", app_id="app-a", url="https://example.test/",
          output_dir=Path(directory) / "encoded", config={})
      try:
        subscription = broker.event_hub.subscribe(BrokerAuth("app", "app-a"))
        broker.event_hub.add_job(subscription, job, 0)
        broker.event_hub.publish(job, "sample", {"value": "payload"})
        message = subscription.pop(0)
        self.assertIsNotNone(message)
        self.assertEqual(message.value, json.loads(message.payload))
        self.assertEqual(0, subscription.message_bytes)
      finally:
        broker.close()

  def test_persistence_failure_is_visible_and_rejects_later_events(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory), token="test", start_scheduler=False))
      job = Job(
          id="storage", app_id="app-a", url="https://example.test/",
          output_dir=Path(directory) / "storage", config={})
      try:
        with mock.patch.object(
            broker.event_writer, "_write_batch", side_effect=OSError("disk full")):
          broker.add_event(job, "sample", {"value": 1})
          with self.assertRaisesRegex(RuntimeError, "disk full"):
            broker.event_writer.flush()
        self.assertIn("disk full", broker.event_writer.failure)
        with self.assertRaisesRegex(RuntimeError, "event persistence failed"):
          broker.add_event(job, "sample", {"value": 2})
      finally:
        broker.close()

  def test_batched_persistence_is_lossless_and_ordered(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory), token="test", start_scheduler=False))
      job = Job(
          id="persisted", app_id="app-a", url="https://example.test/",
          output_dir=Path(directory) / "persisted", config={})
      try:
        for value in range(100):
          broker.add_event(job, "sample", {"value": value})
        broker.event_writer.flush()
        lines = (job.output_dir / "events.jsonl").read_text(encoding="utf-8")
        events = [json.loads(line) for line in lines.splitlines()]
        self.assertEqual(list(range(1, 101)), [
            event["sequence"] for event in events])
      finally:
        broker.close()

  def test_concurrent_job_events_keep_cursor_and_jsonl_in_sequence(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory), token="test", start_scheduler=False))
      job = Job(
          id="concurrent", app_id="app-a", url="https://example.test/",
          output_dir=Path(directory) / "concurrent", config={})
      barrier = threading.Barrier(8)

      def publish(worker: int) -> None:
        barrier.wait()
        for value in range(50):
          broker.add_event(job, "sample", {"worker": worker, "value": value})

      threads = [
          threading.Thread(target=publish, args=(worker,))
          for worker in range(8)
      ]
      try:
        for thread in threads:
          thread.start()
        for thread in threads:
          thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        broker.event_writer.flush()
        events = [json.loads(line) for line in (
            job.output_dir / "events.jsonl").read_text(
                encoding="utf-8").splitlines()]
        self.assertEqual(400, job.event_sequence)
        self.assertEqual(list(range(1, 401)), [
            event["sequence"] for event in events])
      finally:
        broker.close()


if __name__ == "__main__":
  unittest.main()
