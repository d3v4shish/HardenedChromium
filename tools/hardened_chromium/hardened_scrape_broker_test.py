#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from hardened_scrape_broker import Broker, BrokerConfig, Job, cdp_create_tab


class BrokerSchedulingTest(unittest.TestCase):

  def make_broker(self, root: Path) -> Broker:
    return Broker(BrokerConfig(
        cdp_endpoint="http://127.0.0.1:1",
        output_root=root,
        token="test",
        max_active_jobs=2,
        max_active_jobs_per_app=1,
        start_scheduler=False))

  def add_queued(self, broker: Broker, job_id: str, app_id: str) -> Job:
    job = Job(
        id=job_id,
        app_id=app_id,
        url="https://example.test/",
        output_dir=broker.config.output_root / app_id / job_id,
        config={"checkpoint_items": 1},
        status="queued")
    broker.jobs[job.id] = job
    broker.enqueue_job(job, persist=False)
    return job

  def test_round_robin_and_per_app_limit(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = self.make_broker(Path(directory))
      self.add_queued(broker, "a1", "app-a")
      self.add_queued(broker, "a2", "app-a")
      self.add_queued(broker, "b1", "app-b")

      with broker.scheduler_condition:
        first = broker.next_schedulable_job_locked()
        self.assertEqual("a1", first.id)
        broker.active_jobs += 1
        broker.active_by_app[first.app_id] += 1
        second = broker.next_schedulable_job_locked()
        self.assertEqual("b1", second.id)
        broker.active_jobs += 1
        broker.active_by_app[second.app_id] += 1
        self.assertIsNone(broker.next_schedulable_job_locked())

  def test_queued_job_survives_restart(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      state = root / "_broker_state"
      state.mkdir(parents=True)
      (state / "jobs.json").write_text(json.dumps({
          "jobs": [{
              "id": "queued-job",
              "appId": "app-a",
              "url": "https://example.test/",
              "outputDir": str(root / "app-a" / "queued-job"),
              "config": {"checkpoint_items": 1},
              "status": "queued",
          }],
      }), encoding="utf-8")
      broker = self.make_broker(root)
      self.assertEqual("queued", broker.get_job("queued-job").status)
      self.assertEqual(["queued-job"], list(broker.queued_by_app["app-a"]))

  def test_active_job_becomes_interrupted_after_restart(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      state = root / "_broker_state"
      state.mkdir(parents=True)
      (state / "jobs.json").write_text(json.dumps({
          "jobs": [{
              "id": "running-job",
              "appId": "app-a",
              "url": "https://example.test/",
              "outputDir": str(root / "app-a" / "running-job"),
              "config": {"checkpoint_items": 1},
              "status": "running",
          }],
      }), encoding="utf-8")
      broker = self.make_broker(root)
      job = broker.get_job("running-job")
      self.assertEqual("interrupted", job.status)
      self.assertEqual("broker_restarted", job.reason)

  def test_stopped_scheduled_job_never_opens_browser_tab(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = self.make_broker(Path(directory))
      job = self.add_queued(broker, "stopped-before-start", "app-a")
      job.status = "scheduled"
      job.stop_event.set()

      with mock.patch(
          "hardened_scrape_broker.cdp_create_tab") as create_tab:
        broker.run_job(job)

      create_tab.assert_not_called()
      self.assertEqual("stopped", job.status)
      self.assertEqual("stopped_before_start", job.reason)

  def test_tab_open_failure_is_audited_without_opening_a_tab(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = self.make_broker(Path(directory))
      job = self.add_queued(broker, "failed-open", "app-a")
      job.status = "scheduled"

      with mock.patch(
          "hardened_scrape_broker.cdp_create_tab",
          side_effect=RuntimeError("CDP unavailable")):
        broker.run_job(job)

      events, expired = broker.event_hub.events_after(job.id, 0)
      self.assertFalse(expired)
      self.assertIn("tab_opening", [event["type"] for event in events])
      self.assertIn("tab_open_failed", [event["type"] for event in events])
      self.assertEqual("failed", job.status)

  def test_live_scheduler_never_exceeds_global_or_per_app_limits(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      observed_lock = threading.Lock()
      active_by_app: dict[str, int] = {}
      active_total = 0
      maximum_total = 0
      maximum_by_app: dict[str, int] = {}
      starts: list[str] = []

      def fake_run(_broker: Broker, job: Job) -> None:
        nonlocal active_total, maximum_total
        with observed_lock:
          active_total += 1
          active_by_app[job.app_id] = active_by_app.get(job.app_id, 0) + 1
          maximum_total = max(maximum_total, active_total)
          maximum_by_app[job.app_id] = max(
              maximum_by_app.get(job.app_id, 0), active_by_app[job.app_id])
          starts.append(job.app_id)
        job.set_status("running")
        time.sleep(0.03)
        job.set_status("completed")
        with observed_lock:
          active_total -= 1
          active_by_app[job.app_id] -= 1

      with mock.patch.object(Broker, "run_job", fake_run):
        broker = Broker(BrokerConfig(
            cdp_endpoint="http://127.0.0.1:1",
            output_root=root,
            token="test",
            max_active_jobs=3,
            max_active_jobs_per_app=1))
        jobs = [
            self.add_queued(broker, f"{app}-{index}", app)
            for index in range(3)
            for app in ("app-a", "app-b", "app-c", "app-d")
        ]
        deadline = time.monotonic() + 5
        while (any(job.status != "completed" for job in jobs) and
               time.monotonic() < deadline):
          time.sleep(0.01)

      self.assertTrue(all(job.status == "completed" for job in jobs))
      self.assertLessEqual(maximum_total, 3)
      self.assertTrue(all(value <= 1 for value in maximum_by_app.values()))
      self.assertEqual(3, len(set(starts[:3])))


class BrokerOwnershipTest(unittest.TestCase):

  def test_app_cannot_read_or_replace_another_apps_schema(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      broker = Broker(BrokerConfig(
          cdp_endpoint="http://127.0.0.1:1",
          output_root=Path(directory),
          token="test",
          start_scheduler=False))
      request = {
          "id": "shared-name",
          "name": "Owned schema",
          "itemRoot": "article",
          "fields": [{"name": "title", "selector": "h1"}],
      }
      broker.save_schema(request, "app-a")
      self.assertIsNone(broker.get_schema("shared-name", "app-b"))
      with self.assertRaisesRegex(ValueError, "owned by another app"):
        broker.save_schema(request, "app-b")


class SharedBrowserTargetTest(unittest.TestCase):

  def test_jobs_create_tabs_on_the_same_backend_endpoint(self) -> None:
    endpoint = "http://127.0.0.1:43123"
    targets = [
        {"id": "target-one", "webSocketDebuggerUrl": "ws://one"},
        {"id": "target-two", "webSocketDebuggerUrl": "ws://two"},
    ]
    with mock.patch(
        "hardened_scrape_broker.cdp_http_json",
        side_effect=targets) as request:
      first = cdp_create_tab(endpoint, "https://one.example/")
      second = cdp_create_tab(endpoint, "https://two.example/")

    self.assertEqual("target-one", first["id"])
    self.assertEqual("target-two", second["id"])
    self.assertEqual(endpoint, request.call_args_list[0].args[0])
    self.assertEqual(endpoint, request.call_args_list[1].args[0])
    self.assertTrue(request.call_args_list[0].args[1].startswith("/json/new?"))
    self.assertTrue(request.call_args_list[1].args[1].startswith("/json/new?"))
    self.assertEqual("PUT", request.call_args_list[0].args[2])
    self.assertEqual("PUT", request.call_args_list[1].args[2])


if __name__ == "__main__":
  unittest.main()
