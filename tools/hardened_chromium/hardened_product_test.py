#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from hardened_product import ProductError, claim_profile, verify_binary_product


TOOLS_DIR = Path(__file__).resolve().parent


class ProductContractTest(unittest.TestCase):

  def make_binary(self, root: Path, product: str) -> Path:
    binary = root / "chrome"
    binary.write_bytes(b"test browser")
    binary.chmod(0o700)
    (root / "hardened-build-manifest.json").write_text(json.dumps({
        "schemaVersion": 1,
        "product": product,
        "boundary": "red" if product == "privacy" else "blue",
        "remoteDebugging": product == "automation",
        "adapterPackEligible": product == "automation",
        "built": True,
        "binarySha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    return binary

  def test_binary_cannot_cross_product_boundary(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      binary = self.make_binary(Path(directory), "privacy")
      with self.assertRaisesRegex(ProductError, "refusing to launch"):
        verify_binary_product(binary, "automation")

  def test_binary_resolver_keeps_product_output_paths_distinct(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      for product in ("Privacy", "Automation"):
        binary = root / f"out/Hardened{product}/chrome"
        binary.parent.mkdir(parents=True)
        binary.write_text("fixture", encoding="utf-8")
        binary.chmod(0o700)
      environment = os.environ.copy()
      environment["HARDENED_CHROMIUM_CPU_VARIANT"] = "portable"
      environment.pop("HARDENED_CHROMIUM_BINARY", None)
      resolved = {}
      for product in ("privacy", "automation"):
        completed = subprocess.run(
            ["bash", "-c", 'source "$1"; '
             'resolve_hardened_chromium_binary "$2" "$3"', "test",
             str(TOOLS_DIR / "performance_binary.sh"), str(root), product],
            env=environment, capture_output=True, text=True, timeout=5,
            check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)
        resolved[product] = Path(completed.stdout.strip())
      self.assertEqual(root / "out/HardenedPrivacy/chrome",
                       resolved["privacy"])
      self.assertEqual(root / "out/HardenedAutomation/chrome",
                       resolved["automation"])
      self.assertNotEqual(resolved["privacy"], resolved["automation"])

  def test_checksum_mismatch_is_rejected(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      binary = self.make_binary(Path(directory), "automation")
      binary.write_bytes(b"changed")
      with self.assertRaisesRegex(ProductError, "checksum"):
        verify_binary_product(binary, "automation")

  def test_incomplete_build_manifest_is_rejected(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      binary = self.make_binary(root, "privacy")
      manifest_path = root / "hardened-build-manifest.json"
      manifest = json.loads(manifest_path.read_text())
      manifest["built"] = False
      manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
      with self.assertRaisesRegex(ProductError, "not marked complete"):
        verify_binary_product(binary, "privacy")

  def test_profile_roles_are_isolated_by_default(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      profile = Path(directory) / "profile"
      claim_profile(profile, "privacy")
      with self.assertRaisesRegex(ProductError, "belongs to privacy"):
        claim_profile(profile, "automation")
      shared = claim_profile(profile, "automation", allow_sharing=True)
      self.assertTrue(shared["sharedOverride"])
      marker = json.loads((profile / ".hardened-profile.json").read_text())
      self.assertEqual("privacy", marker["product"])

  def test_active_profile_rejects_concurrent_cross_product_owner(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      profile = Path(directory) / "profile"
      profile.mkdir()
      lock_path = profile / ".hardened-product.lock"
      with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0)
        lock.truncate()
        lock.write("privacy\n")
        lock.flush()
        completed = subprocess.run(
            ["bash", "-c", 'source "$1"; '
             'claim_hardened_profile_process_lock "$2" automation', "test",
             str(TOOLS_DIR / "performance_binary.sh"), str(profile)],
            capture_output=True, text=True, timeout=5, check=False)
      self.assertNotEqual(0, completed.returncode)
      self.assertIn("cross-product", completed.stderr)

  def test_profile_process_lock_rejects_symlink(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      profile = root / "profile"
      profile.mkdir()
      (profile / ".hardened-product.lock").symlink_to(root / "elsewhere")
      completed = subprocess.run(
          ["bash", "-c", 'source "$1"; '
           'claim_hardened_profile_process_lock "$2" privacy', "test",
           str(TOOLS_DIR / "performance_binary.sh"), str(profile)],
          capture_output=True, text=True, timeout=5, check=False)
      self.assertNotEqual(0, completed.returncode)
      self.assertIn("must not be a symlink", completed.stderr)

  def test_active_profile_allows_same_product_singleton_request(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      profile = Path(directory) / "profile"
      profile.mkdir()
      lock_path = profile / ".hardened-product.lock"
      with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write("automation\n")
        lock.flush()
        completed = subprocess.run(
            ["bash", "-c", 'source "$1"; '
             'claim_hardened_profile_process_lock "$2" automation', "test",
             str(TOOLS_DIR / "performance_binary.sh"), str(profile)],
            capture_output=True, text=True, timeout=5, check=False)
      self.assertEqual(0, completed.returncode, completed.stderr)

  def test_privacy_launcher_defaults_all_physical_sources_to_fake(self) -> None:
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
          "HARDENED_PRIVACY_PROFILE": str(root / "profile"),
          "HARDENED_ALLOW_UNVERIFIED_BINARY": "1",
      })
      completed = subprocess.run(
          [str(TOOLS_DIR / "run_privacy.sh"), "about:blank"],
          env=environment, capture_output=True, text=True, timeout=10,
          check=False)
      self.assertEqual(0, completed.returncode, completed.stderr)
      arguments = arguments_file.read_text(encoding="utf-8").splitlines()
      self.assertIn("--hardened-selectable-media-sources", arguments)
      self.assertIn("--hardened-default-camera-source=fake", arguments)
      self.assertIn("--hardened-default-microphone-source=fake", arguments)
      self.assertIn("--hardened-default-location-source=fake", arguments)
      self.assertFalse(any(
          argument.startswith("--remote-debugging-") for argument in arguments))


if __name__ == "__main__":
  unittest.main()
