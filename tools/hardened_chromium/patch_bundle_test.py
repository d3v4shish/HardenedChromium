#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parents[2]
PATCH_DIR = SOURCE_DIR / "patches"
BASE_REVISION = "f77d44b339946cd682d311c6c0bc922c32579fbd"
APPLY_SPEC = importlib.util.spec_from_file_location(
    "hardened_patch_apply", PATCH_DIR / "apply.py")
assert APPLY_SPEC and APPLY_SPEC.loader
PATCH_APPLY = importlib.util.module_from_spec(APPLY_SPEC)
APPLY_SPEC.loader.exec_module(PATCH_APPLY)
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "hardened_patch_generator",
    SOURCE_DIR / "tools/hardened_chromium/generate_patch_manifests.py")
assert GENERATOR_SPEC and GENERATOR_SPEC.loader
PATCH_GENERATOR = importlib.util.module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(PATCH_GENERATOR)


class PatchBundleTest(unittest.TestCase):

  def load(self, bundle: str) -> dict[str, object]:
    return json.loads(
        (PATCH_DIR / bundle / "manifest.json").read_text(encoding="utf-8"))

  def test_payloads_are_current_checked_and_disjoint(self) -> None:
    manifests = {name: self.load(name) for name in ("privacy", "automation")}
    all_paths: set[str] = set()
    for name, manifest in manifests.items():
      self.assertEqual(1, manifest["schemaVersion"])
      self.assertEqual(name, manifest["id"])
      self.assertEqual(BASE_REVISION, manifest["baseRevision"])
      for raw in manifest["files"]:
        path = str(raw["path"])
        self.assertNotIn(path, all_paths)
        all_paths.add(path)
        actual = hashlib.sha256((SOURCE_DIR / path).read_bytes()).hexdigest()
        self.assertEqual(raw["sha256"], actual, path)
      self.assertEqual(
          manifest["payloadSha256"],
          PATCH_APPLY.manifest_payload_digest(manifest["files"]))
    expected_paths = set(PATCH_GENERATOR.changed_paths())
    self.assertEqual(expected_paths, all_paths)
    for path in all_paths:
      expected_bundle = PATCH_GENERATOR.bundle_for(path)
      manifest_paths = {
          str(raw["path"]) for raw in manifests[expected_bundle]["files"]}
      self.assertIn(path, manifest_paths)

  def test_automation_requires_privacy_and_privacy_has_no_broker(self) -> None:
    privacy = self.load("privacy")
    automation = self.load("automation")
    privacy_paths = {str(raw["path"]) for raw in privacy["files"]}
    automation_paths = {str(raw["path"]) for raw in automation["files"]}
    self.assertEqual(["privacy"], automation["requires"])
    self.assertNotIn(
        "tools/hardened_chromium/hardened_scrape_broker.py", privacy_paths)
    self.assertIn(
        "tools/hardened_chromium/hardened_scrape_broker.py", automation_paths)
    self.assertNotIn(
        "build/config/hardened_chromium/automation.gni", privacy_paths)
    self.assertIn(
        "build/config/hardened_chromium/automation.gni", automation_paths)
    self.assertNotIn(
        "tools/hardened_chromium/product_smoke.py", privacy_paths)
    self.assertIn(
        "tools/hardened_chromium/product_smoke.py", automation_paths)
    self.assertTrue(any(
        path.startswith("tools/hardened_chromium/adapter_packs/")
        for path in automation_paths))

  def test_apply_rejects_duplicate_manifest_paths(self) -> None:
    entries = [
        {"path": "README.md", "sha256": "a" * 64,
         "baseSha256": None, "mode": 0o644},
        {"path": "README.md", "sha256": "b" * 64,
         "baseSha256": None, "mode": 0o644},
    ]
    with self.assertRaisesRegex(PATCH_APPLY.PatchError, "duplicate path"):
      PATCH_APPLY.manifest_payload_digest(entries)

  def test_apply_rejects_invalid_file_mode(self) -> None:
    with self.assertRaisesRegex(PATCH_APPLY.PatchError, "invalid mode"):
      PATCH_APPLY.manifest_payload_digest([{
          "path": "README.md",
          "sha256": "a" * 64,
          "baseSha256": None,
          "mode": 0o4755,
      }])

  def test_apply_rejects_symlink_destination(self) -> None:
    source = SOURCE_DIR / "README.md"
    with tempfile.TemporaryDirectory() as directory:
      target = Path(directory)
      victim = target / "victim"
      victim.write_text("unchanged", encoding="utf-8")
      (target / "README.md").symlink_to(victim)
      raw = {
          "path": "README.md",
          "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
          "baseSha256": None,
          "mode": 0o644,
      }
      with self.assertRaisesRegex(PATCH_APPLY.PatchError, "symlink"):
        PATCH_APPLY.verify_entry(target, raw)
      self.assertEqual("unchanged", victim.read_text(encoding="utf-8"))

  def test_apply_rejects_wrong_applied_file_mode(self) -> None:
    source = SOURCE_DIR / "README.md"
    with tempfile.TemporaryDirectory() as directory:
      target = Path(directory)
      destination = target / "README.md"
      destination.write_bytes(source.read_bytes())
      destination.chmod(0o600)
      raw = {
          "path": "README.md",
          "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
          "baseSha256": None,
          "mode": 0o644,
      }
      with self.assertRaisesRegex(PATCH_APPLY.PatchError, "unexpected mode"):
        PATCH_APPLY.verify_entry(target, raw)

  def test_bundles_apply_in_order_to_pinned_base_fixture(self) -> None:
    manifests = {name: self.load(name) for name in ("privacy", "automation")}
    with tempfile.TemporaryDirectory() as directory:
      target = Path(directory) / "source"
      subprocess.run(["git", "init", "--quiet", str(target)], check=True)
      alternates = target / ".git/objects/info/alternates"
      alternates.parent.mkdir(parents=True, exist_ok=True)
      alternates.write_text(
          str((SOURCE_DIR / ".git/objects").resolve()) + "\n",
          encoding="utf-8")
      source_revision = subprocess.run(
          ["git", "rev-parse", "HEAD"], cwd=SOURCE_DIR, check=True,
          text=True, stdout=subprocess.PIPE).stdout.strip()
      subprocess.run([
          "git", "-C", str(target), "update-ref", "HEAD", source_revision,
      ], check=True)
      with self.assertRaisesRegex(
          PATCH_APPLY.PatchError, "target revision mismatch"):
        PATCH_APPLY.apply_bundle("privacy", target)
      subprocess.run([
          "git", "-C", str(target), "update-ref", "HEAD", BASE_REVISION,
      ], check=True)
      for manifest in manifests.values():
        for raw in manifest["files"]:
          original = PATCH_GENERATOR.base_bytes(str(raw["path"]))
          if original is None:
            continue
          destination = target / str(raw["path"])
          destination.parent.mkdir(parents=True, exist_ok=True)
          destination.write_bytes(original)

      state_dir = target / PATCH_APPLY.STATE_DIR
      state_dir.mkdir()
      state_victim = target / "state-victim"
      state_victim.write_text("unchanged", encoding="utf-8")
      privacy_state = state_dir / "privacy.json"
      privacy_state.symlink_to(state_victim)
      readme_before = (target / "README.md").read_bytes()
      with self.assertRaisesRegex(PATCH_APPLY.PatchError, "state file"):
        PATCH_APPLY.apply_bundle("privacy", target)
      self.assertEqual(readme_before, (target / "README.md").read_bytes())
      self.assertEqual("unchanged", state_victim.read_text(encoding="utf-8"))
      privacy_state.unlink()

      with contextlib.redirect_stdout(io.StringIO()):
        PATCH_APPLY.apply_bundle("privacy", target)
        PATCH_APPLY.apply_bundle("automation", target)
        PATCH_APPLY.apply_bundle("automation", target, check_only=True)

      for manifest in manifests.values():
        for raw in manifest["files"]:
          actual = hashlib.sha256(
              (target / str(raw["path"])).read_bytes()).hexdigest()
          self.assertEqual(raw["sha256"], actual, raw["path"])


if __name__ == "__main__":
  unittest.main()
