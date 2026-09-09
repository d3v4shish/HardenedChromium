#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Regression tests for user-local broker discovery installation."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from hardened_scrape_client import (
    AutoBrokerClient,
    DEFAULT_SERVICE_SCRIPT,
    resolve_service_script,
)
from hardened_scrape_service import (
    SERVICE_BACKEND_ID,
    SERVICE_PROTOCOL_VERSION,
    ServiceConfig,
    capabilities_document,
)


TOOLS_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = TOOLS_DIR.parent.parent
INSTALLER = TOOLS_DIR / "install_hardened_chromium_service.py"
PRIVACY_DESKTOP_INSTALLER = TOOLS_DIR / "install_red_desktop_entry.sh"
AUTOMATION_DESKTOP_INSTALLER = (
    TOOLS_DIR / "install_green_automation_desktop_entry.sh")


def write_product_manifest(binary: Path, product: str = "automation") -> None:
  (binary.parent / "hardened-build-manifest.json").write_text(json.dumps({
      "schemaVersion": 1,
      "product": product,
      "boundary": "blue" if product == "automation" else "red",
      "remoteDebugging": product == "automation",
      "adapterPackEligible": product == "automation",
      "built": True,
      "binarySha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
  }), encoding="utf-8")


class ClientServiceDiscoveryTest(unittest.TestCase):

  def test_explicit_path_beats_environment_and_path_lookup(self) -> None:
    with (mock.patch.dict("os.environ", {
        "HARDENED_SCRAPE_SERVICE": "/configured/service"}),
          mock.patch("hardened_scrape_client.shutil.which",
                     return_value="/path/service")):
      self.assertEqual(
          Path("/explicit/service"), resolve_service_script("/explicit/service"))

  def test_environment_then_path_then_adjacent_fallback(self) -> None:
    with mock.patch.dict("os.environ", {
        "HARDENED_SCRAPE_SERVICE": "/configured/service"}):
      self.assertEqual(Path("/configured/service"), resolve_service_script())
    with (mock.patch.dict("os.environ", {}, clear=True),
          mock.patch("hardened_scrape_client.shutil.which",
                     return_value="/path/service")):
      self.assertEqual(Path("/path/service"), resolve_service_script())
    with (mock.patch.dict("os.environ", {}, clear=True),
          mock.patch("hardened_scrape_client.shutil.which", return_value=None)):
      self.assertEqual(DEFAULT_SERVICE_SCRIPT, resolve_service_script())

  def test_auto_client_ensures_before_submitting_new_browser_work(self) -> None:
    service = {
        "ok": True,
        "broker": {"url": "http://127.0.0.1:8877"},
        "token": "test-token",
    }
    with (mock.patch("hardened_scrape_client.ensure_service",
                     return_value=service) as ensure,
          mock.patch("hardened_scrape_client.BrokerClient.submit_job",
                     return_value={"ok": True}) as submit):
      client = AutoBrokerClient(app_id="test-app")
      client.submit_job("https://example.test/")
    self.assertEqual(2, ensure.call_count)
    submit.assert_called_once()


class CapabilityContractTest(unittest.TestCase):

  def test_capabilities_are_versioned_and_disclose_no_runtime_secrets(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      binary = root / "chrome"
      binary.write_bytes(b"development fixture")
      binary.chmod(0o700)
      config = ServiceConfig(
          cdp_endpoint="auto",
          broker_url="http://127.0.0.1:8877",
          state_dir=root / "state",
          output_root=root / "output",
          launcher=TOOLS_DIR / "run_for_automation.sh",
          broker_script=TOOLS_DIR / "hardened_scrape_broker.py",
          python=sys.executable,
          profile=root / "profile",
          timeout_seconds=1,
          no_auth=False)
      with mock.patch.dict("os.environ", {
          "HARDENED_CHROMIUM_BINARY": str(binary),
          "HARDENED_ALLOW_UNVERIFIED_BINARY": "1",
      }):
        payload = capabilities_document(config)
    serialized = json.dumps(payload).lower()
    self.assertTrue(payload["ok"])
    self.assertEqual(SERVICE_BACKEND_ID, payload["backend"])
    self.assertEqual(SERVICE_PROTOCOL_VERSION, payload["protocolVersion"])
    self.assertNotIn("token", serialized)
    self.assertNotIn("devtools", serialized)
    self.assertNotIn("cdp", serialized)


class InstallerIntegrationTest(unittest.TestCase):

  def test_desktop_installers_create_distinct_product_entries(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      data_home = root / "share"
      privacy_binary = root / "privacy" / "chrome"
      automation_binary = root / "automation" / "chrome"
      for binary, product in (
          (privacy_binary, "privacy"),
          (automation_binary, "automation"),
      ):
        binary.parent.mkdir(parents=True)
        binary.write_bytes(product.encode("ascii"))
        binary.chmod(0o700)
        write_product_manifest(binary, product)

      environment = os.environ.copy()
      environment["XDG_DATA_HOME"] = str(data_home)
      for installer, binary in (
          (PRIVACY_DESKTOP_INSTALLER, privacy_binary),
          (AUTOMATION_DESKTOP_INSTALLER, automation_binary),
      ):
        environment["HARDENED_CHROMIUM_BINARY"] = str(binary)
        completed = subprocess.run(
            [str(installer)], env=environment, capture_output=True, text=True,
            timeout=10, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)

      privacy_entry = (data_home / "applications" /
                       "hardened-chromium-privacy.desktop").read_text()
      automation_entry = (data_home / "applications" /
                          "hardened-chromium-automation.desktop").read_text()
      self.assertIn("Name=Hardened Chromium Privacy", privacy_entry)
      self.assertIn("StartupWMClass=HardenedChromiumPrivacy", privacy_entry)
      self.assertIn(str(privacy_binary), privacy_entry)
      self.assertIn("hardened-chromium-privacy.svg", privacy_entry)
      self.assertIn("Name=Hardened Chromium Automation", automation_entry)
      self.assertIn(
          "StartupWMClass=HardenedChromiumAutomation", automation_entry)
      self.assertIn(str(automation_binary), automation_entry)
      self.assertIn("hardened-chromium-automation.svg", automation_entry)

      privacy_icon = (TOOLS_DIR / "icons" /
                      "hardened-chromium-privacy.svg").read_text()
      automation_icon = (TOOLS_DIR / "icons" /
                         "hardened-chromium-automation.svg").read_text()
      self.assertIn("#D21919", privacy_icon)
      self.assertNotIn("#188038", privacy_icon)
      self.assertIn("#188038", automation_icon)
      self.assertNotIn("#D21919", automation_icon)
      self.assertEqual(
          privacy_icon,
          (data_home / "icons/hicolor/scalable/apps" /
           "hardened-chromium-privacy.svg").read_text())
      self.assertEqual(
          automation_icon,
          (data_home / "icons/hicolor/scalable/apps" /
           "hardened-chromium-automation.svg").read_text())

  def test_installed_wrapper_reports_capabilities_and_uninstalls_safely(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      prefix = root / "prefix"
      config_file = root / "config" / "installation.json"
      binary = root / "chrome"
      binary.write_bytes(b"development fixture")
      binary.chmod(0o700)
      write_product_manifest(binary)
      environment = os.environ.copy()
      environment["HARDENED_CHROMIUM_BINARY"] = str(binary)
      installed = subprocess.run(
          [sys.executable, str(INSTALLER), "--source-root", str(SOURCE_ROOT),
           "--prefix", str(prefix), "--config-file", str(config_file), "--json"],
          env=environment, capture_output=True, text=True, timeout=10,
          check=False)
      self.assertEqual(0, installed.returncode, installed.stderr)
      wrapper = prefix / "bin" / "hardened-chromium-service"
      state_dir = root / "unused-state"
      environment.pop("HARDENED_CHROMIUM_BINARY")
      environment.pop("HARDENED_ALLOW_UNVERIFIED_BINARY", None)
      environment["HARDENED_SCRAPE_STATE_DIR"] = str(state_dir)
      self.assertTrue(wrapper.is_file())
      capabilities = subprocess.run(
          [str(wrapper), "capabilities", "--json"],
          env=environment, capture_output=True, text=True, timeout=10,
          check=False)
      self.assertEqual(0, capabilities.returncode, capabilities.stderr)
      payload = json.loads(capabilities.stdout)
      self.assertTrue(payload["ok"])
      self.assertEqual(str(wrapper), payload["command"])
      self.assertEqual(str(binary), payload["installation"]["chromiumBinary"])
      self.assertFalse(state_dir.exists())
      removed = subprocess.run(
          [sys.executable, str(INSTALLER), "--prefix", str(prefix),
           "--config-file", str(config_file), "--uninstall", "--json"],
          capture_output=True, text=True, timeout=10, check=False)
      self.assertEqual(0, removed.returncode, removed.stderr)
      self.assertFalse(wrapper.exists())
      self.assertFalse(config_file.exists())

  def test_installer_refuses_to_overwrite_an_unrelated_command(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      prefix = root / "prefix"
      command = prefix / "bin" / "hardened-chromium-service"
      command.parent.mkdir(parents=True)
      command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
      command.chmod(0o755)
      binary = root / "chrome"
      binary.write_bytes(b"development fixture")
      binary.chmod(0o700)
      environment = os.environ.copy()
      environment["HARDENED_CHROMIUM_BINARY"] = str(binary)
      environment["HARDENED_ALLOW_UNVERIFIED_BINARY"] = "1"
      completed = subprocess.run(
          [sys.executable, str(INSTALLER), "--source-root", str(SOURCE_ROOT),
           "--prefix", str(prefix), "--json"],
          env=environment, capture_output=True, text=True, timeout=10,
          check=False)
      self.assertNotEqual(0, completed.returncode)
      self.assertIn("refusing to overwrite", completed.stdout)


if __name__ == "__main__":
  unittest.main()
