#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from configure_performance_builds import (
    SOURCE_DIR,
    gn_args,
    output_directory,
    write_manifest,
)


class ProductBuildConfigurationTest(unittest.TestCase):

  def test_products_have_distinct_output_directories_and_gn_args(self) -> None:
    profile = SOURCE_DIR / "chrome/VERSION"
    privacy_args = gn_args(profile, "privacy", "portable")
    automation_args = gn_args(profile, "automation", "portable")
    self.assertIn('hardened_chromium_variant = "privacy"', privacy_args)
    self.assertIn('hardened_chromium_variant = "automation"', automation_args)
    self.assertNotEqual(
        output_directory("privacy", "portable"),
        output_directory("automation", "portable"))

  def test_completed_manifest_identifies_and_checksums_product(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      output = Path(directory)
      binary = output / "chrome"
      binary.write_bytes(b"automation fixture")
      with mock.patch(
          "configure_performance_builds.source_revision",
          return_value="revision"):
        write_manifest(
            output, "automation", "portable", SOURCE_DIR / "chrome/VERSION",
            built=True)
      manifest = json.loads(
          (output / "hardened-build-manifest.json").read_text())
      self.assertEqual("automation", manifest["product"])
      self.assertEqual("blue", manifest["boundary"])
      self.assertTrue(manifest["remoteDebugging"])
      self.assertTrue(manifest["adapterPackEligible"])
      self.assertTrue(manifest["built"])
      self.assertEqual(
          hashlib.sha256(binary.read_bytes()).hexdigest(),
          manifest["binarySha256"])

  def test_completed_manifest_requires_binary(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      with self.assertRaisesRegex(FileNotFoundError, "binary is missing"):
        write_manifest(
            Path(directory), "privacy", "portable",
            SOURCE_DIR / "chrome/VERSION", built=True)


if __name__ == "__main__":
  unittest.main()
