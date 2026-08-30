#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from pathlib import Path
import os
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from hardened_scrape_service import (
    HttpProbe,
    ServiceConfig,
    ServiceError,
    broker_probe,
    clear_stale_browser_runtime_files,
    discover_cdp_endpoint,
    diagnostics_document,
    ensure_service,
    profile_locked_by_other_browser,
    start_browser,
    stop_service,
    stop_shared_browser,
    terminate_owned_pid,
)


class ServiceOwnershipTest(unittest.TestCase):

  def make_config(self, root: Path) -> ServiceConfig:
    return ServiceConfig(
        cdp_endpoint="auto",
        broker_url="http://127.0.0.1:8877",
        state_dir=root / "state",
        output_root=(root / "outputs").resolve(),
        launcher=root / "launcher",
        broker_script=root / "broker.py",
        python="python3",
        profile=root / "profile",
        timeout_seconds=1,
        no_auth=True)

  def test_discovers_ephemeral_loopback_endpoint(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      config.profile.mkdir(parents=True)
      (config.profile / "DevToolsActivePort").write_text(
          "43123\n/devtools/browser/id\n", encoding="utf-8")
      self.assertEqual(
          "http://127.0.0.1:43123", discover_cdp_endpoint(config))

  def test_ensure_reuses_healthy_browser_and_broker(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      healthy = HttpProbe(True, 200, {"ok": True})
      with (mock.patch("hardened_scrape_service.broker_probe",
                       return_value=healthy),
            mock.patch("hardened_scrape_service.cdp_probe",
                       return_value=healthy),
            mock.patch("hardened_scrape_service.start_browser") as browser,
            mock.patch("hardened_scrape_service.start_broker") as broker,
            mock.patch("hardened_scrape_service.status_document",
                       return_value={"ok": True}) as status):
        result = ensure_service(config)
      self.assertTrue(result["ok"])
      self.assertEqual(
          {"browser": False, "broker": False}, result["started"])
      browser.assert_not_called()
      broker.assert_not_called()
      status.assert_called_once_with(config, "", healthy, healthy)

  def test_ensure_recovers_browser_and_recycles_its_old_broker(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      state = {"browser": False, "broker": True}

      def cdp_probe(_config):
        return HttpProbe(state["browser"], 200 if state["browser"] else None)

      def broker_probe(_config, _token):
        return HttpProbe(state["broker"], 200 if state["broker"] else None,
                         {"ok": True})

      def start_browser(_config):
        state["browser"] = True
        return 101

      def stop_broker(*_args):
        state["broker"] = False
        return "terminated"

      def start_broker(_config, _token):
        state["broker"] = True
        return 102

      with (mock.patch("hardened_scrape_service.cdp_probe",
                       side_effect=cdp_probe),
            mock.patch("hardened_scrape_service.broker_probe",
                       side_effect=broker_probe),
            mock.patch("hardened_scrape_service.start_browser",
                       side_effect=start_browser) as browser,
            mock.patch("hardened_scrape_service.terminate_owned_pid",
                       side_effect=stop_broker) as terminate,
            mock.patch("hardened_scrape_service.start_broker",
                       side_effect=start_broker) as broker,
            mock.patch("hardened_scrape_service.status_document",
                       return_value={"ok": True})):
        result = ensure_service(config)

      self.assertTrue(result["ok"])
      self.assertEqual({"browser": True, "broker": True}, result["started"])
      browser.assert_called_once_with(config)
      terminate.assert_called_once()
      broker.assert_called_once_with(config, "")

  def test_stale_profile_markers_do_not_block_browser_recovery(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      config.profile.mkdir(parents=True)
      for name in (
          "SingletonLock", "SingletonCookie", "SingletonSocket",
          "DevToolsActivePort"):
        (config.profile / name).write_text("stale", encoding="utf-8")
      with mock.patch("hardened_scrape_service.profile_browser_pid",
                      return_value=None):
        self.assertFalse(profile_locked_by_other_browser(config))
        clear_stale_browser_runtime_files(config)
      self.assertFalse(any((config.profile / name).exists() for name in (
          "SingletonLock", "SingletonCookie", "SingletonSocket",
          "DevToolsActivePort")))

  def test_live_foreign_profile_owner_blocks_recovery(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      config.profile.mkdir(parents=True)
      lock = config.profile / "SingletonLock"
      lock.write_text("owned", encoding="utf-8")
      with mock.patch("hardened_scrape_service.profile_browser_pid",
                      return_value=4242):
        self.assertTrue(profile_locked_by_other_browser(config))
        clear_stale_browser_runtime_files(config)
      self.assertTrue(lock.exists())

  def test_live_profile_with_unreachable_cdp_returns_specific_error(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      config.profile.mkdir(parents=True)
      config.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
      with mock.patch("hardened_scrape_service.profile_browser_pid",
                      return_value=4242):
        with self.assertRaises(ServiceError) as raised:
          start_browser(config)
      self.assertEqual("browser_unreachable", raised.exception.code)
      self.assertIn("private CDP endpoint", str(raised.exception))

  def test_concurrent_ensure_calls_start_each_process_once(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      state_lock = threading.Lock()
      ready = {"browser": False, "broker": False}
      starts = {"browser": 0, "broker": 0}

      def cdp_probe(_config):
        with state_lock:
          is_ready = ready["browser"]
        return HttpProbe(is_ready, 200 if is_ready else None, {"ok": True})

      def broker_probe(_config, _token):
        with state_lock:
          is_ready = ready["broker"]
        return HttpProbe(is_ready, 200 if is_ready else None, {"ok": True})

      def start_browser(_config):
        with state_lock:
          starts["browser"] += 1
          ready["browser"] = True
        return 101

      def start_broker(_config, _token):
        with state_lock:
          starts["broker"] += 1
          ready["broker"] = True
        return 102

      with (mock.patch("hardened_scrape_service.cdp_probe",
                       side_effect=cdp_probe),
            mock.patch("hardened_scrape_service.broker_probe",
                       side_effect=broker_probe),
            mock.patch("hardened_scrape_service.start_browser",
                       side_effect=start_browser),
            mock.patch("hardened_scrape_service.start_broker",
                       side_effect=start_broker),
            mock.patch("hardened_scrape_service.status_document",
                       side_effect=lambda *_args: {"ok": True})):
        barrier = threading.Barrier(2)
        results = []

        def ensure_from_app():
          barrier.wait()
          results.append(ensure_service(config))

        threads = [threading.Thread(target=ensure_from_app) for _ in range(2)]
        for thread in threads:
          thread.start()
        for thread in threads:
          thread.join(timeout=5)

      self.assertEqual(2, len(results))
      self.assertEqual({"browser": 1, "broker": 1}, starts)
      self.assertEqual(
          1, sum(result["started"]["browser"] for result in results))
      self.assertEqual(
          1, sum(result["started"]["broker"] for result in results))

  def test_named_profile_launcher_never_enables_backend(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      arguments_file = root / "arguments.txt"
      fake_binary = root / "fake-chromium"
      fake_binary.write_text(
          "#!/bin/bash\nprintf '%s\\n' \"$@\" > \"${FAKE_ARGUMENTS_FILE}\"\n",
          encoding="utf-8")
      fake_binary.chmod(0o700)
      environment = os.environ.copy()
      environment.update({
          "CHROME_DEVEL_SANDBOX": str(fake_binary),
          "FAKE_ARGUMENTS_FILE": str(arguments_file),
          "HARDENED_CHROMIUM_BINARY": str(fake_binary),
          "HARDENED_CHROMIUM_NAMED_PROFILE_ROOT": str(root / "profiles"),
          "HARDENED_CHROMIUM_RUNTIME_DIR": str(root / "runtime"),
          "HARDENED_MEDIA_MODE": "synthetic",
      })
      launcher = Path(__file__).resolve().with_name("run_for_automation.sh")
      completed = subprocess.run(
          [str(launcher), "--named-profile", "private-test", "about:blank"],
          env=environment,
          capture_output=True,
          text=True,
          timeout=10,
          check=False)
      self.assertEqual(0, completed.returncode, completed.stderr)
      arguments = arguments_file.read_text(encoding="utf-8").splitlines()
      self.assertFalse(any(
          argument.startswith("--remote-debugging-") for argument in arguments))
      self.assertIn("--hardened-default-location-source=fake", arguments)
      self.assertIn("--disable-vulkan", arguments)

  def test_normal_stop_only_terminates_broker(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      config.state_dir.mkdir(parents=True)
      config.broker_pid_file.write_text("123\n", encoding="utf-8")
      with (mock.patch("hardened_scrape_service.terminate_owned_pid",
                       return_value="terminated") as terminate,
            mock.patch("hardened_scrape_service.status_document",
                       return_value={"ok": False})):
        result = stop_service(config)
      self.assertTrue(result["ok"])
      terminate.assert_called_once_with(
          123,
          ["hardened_scrape_broker.py", str(config.output_root)],
          "broker")

  def test_shared_browser_stop_requires_confirmation(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      with self.assertRaisesRegex(ServiceError, "without.*confirm"):
        stop_shared_browser(config, False)

  def test_pid_reuse_cannot_terminate_broker_with_different_root(self) -> None:
    command = (
        "python3 hardened_scrape_broker.py --root /tmp/different-output")
    with (mock.patch("hardened_scrape_service.pid_alive", return_value=True),
          mock.patch("hardened_scrape_service.proc_cmdline",
                     return_value=command),
          mock.patch("hardened_scrape_service.os.kill") as kill):
      result = terminate_owned_pid(
          123,
          ["hardened_scrape_broker.py", "/tmp/expected-output"],
          "broker")
    self.assertTrue(result.startswith("refused_unrecognized_process:"))
    kill.assert_not_called()

  def test_broker_probe_rejects_unrelated_listener(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      with mock.patch(
          "hardened_scrape_service.http_json",
          return_value=HttpProbe(True, 200, {"ok": True})):
        result = broker_probe(config, "")
      self.assertFalse(result.ok)
      self.assertIn("not a Hardened", result.error)

  def test_diagnostics_correlates_app_tab_events_and_redacts_tokens(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      config = self.make_config(Path(directory))
      config.state_dir.mkdir(parents=True)
      config.broker_state_dir.mkdir(parents=True)
      config.browser_log.write_text(
          "FATAL: token=private-value aborted\n", encoding="utf-8")
      config.lifecycle_log.write_text(
          '{"event":"browser_started"}\n', encoding="utf-8")
      (config.broker_state_dir / "events.jsonl").write_text(
          "\n".join((
              '{"type":"job_accepted","time":"now","jobId":"j1",'
              '"appId":"app-a","data":{"host":"example.test"}}',
              '{"type":"tab_opened","time":"now","jobId":"j1",'
              '"appId":"app-a","data":{"targetId":"target-1"}}',
          )) + "\n", encoding="utf-8")
      with mock.patch("hardened_scrape_service.status_document", return_value={
          "ok": True, "token": "private-token",
          "logs": {"browser": "browser", "broker": "broker"}}):
        result = diagnostics_document(config, "")
      self.assertEqual("job_accepted", result["recentAppTabActivity"][0]["type"])
      self.assertEqual("example.test",
                       result["recentAppTabActivity"][0]["data"]["host"])
      self.assertIn("token=REDACTED", result["browserCrashIndicators"][0])
      self.assertNotIn("token", result["status"])


if __name__ == "__main__":
  unittest.main()
