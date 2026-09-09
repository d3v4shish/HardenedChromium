#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Regenerate deterministic Privacy and Automation overlay manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess


SOURCE_DIR = Path(__file__).resolve().parents[2]
BASE_REVISION = "f77d44b339946cd682d311c6c0bc922c32579fbd"


AUTOMATION_ONLY = {
    "build/config/hardened_chromium/automation.gni",
    "third_party/blink/renderer/core/frame/navigator.cc",
    "third_party/blink/renderer/core/BUILD.gn",
    "tools/hardened_chromium/icons/hardened-chromium-automation.svg",
    "tools/hardened_chromium/install_green_automation_desktop_entry.sh",
    "tools/hardened_chromium/product_smoke.py",
}
AUTOMATION_PREFIXES = (
    "tools/hardened_chromium/adapter_packs/",
    "tools/hardened_chromium/benchmark_",
    "tools/hardened_chromium/broker_",
    "tools/hardened_chromium/configure_obs_media.py",
    "tools/hardened_chromium/generate_loop_video.py",
    "tools/hardened_chromium/hardened_adapter_pack",
    "tools/hardened_chromium/hardened_mode_test.html",
    "tools/hardened_chromium/hardened_scrape_",
    "tools/hardened_chromium/install_hardened_chromium_service.py",
    "tools/hardened_chromium/manual_multi_app_test.py",
    "tools/hardened_chromium/performance_fixture.html",
    "tools/hardened_chromium/run_for_automation.sh",
    "tools/hardened_chromium/run_headless_for_automation.sh",
    "tools/hardened_chromium/run_with_virtual_media.sh",
)
EXCLUDED_PREFIXES = (
    ".git/",
    "out/",
    "patches/",
    "tools/hardened_chromium/__pycache__/",
)
EXCLUDED_FILES = {
    "tools/hardened_chromium/generate_patch_manifests.py",
    "tools/hardened_chromium/patch_bundle_test.py",
}


def command(*args: str) -> bytes:
  return subprocess.run(
      list(args), cwd=SOURCE_DIR, check=True, stdout=subprocess.PIPE).stdout


def changed_paths() -> list[str]:
  tracked = command(
      "git", "diff", "--name-only", BASE_REVISION, "--").decode().splitlines()
  untracked = command(
      "git", "ls-files", "--others", "--exclude-standard").decode().splitlines()
  return sorted({path for path in tracked + untracked
                 if path and path not in EXCLUDED_FILES and
                 not path.startswith(EXCLUDED_PREFIXES)})


def bundle_for(path: str) -> str:
  if path in AUTOMATION_ONLY or path.startswith(AUTOMATION_PREFIXES):
    return "automation"
  return "privacy"


def base_bytes(path: str) -> bytes | None:
  completed = subprocess.run(
      ["git", "show", f"{BASE_REVISION}:{path}"], cwd=SOURCE_DIR,
      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
  return completed.stdout if completed.returncode == 0 else None


def entry(path: str) -> dict[str, object]:
  source = SOURCE_DIR / path
  data = source.read_bytes()
  original = base_bytes(path)
  source_mode = stat.S_IMODE(source.stat().st_mode)
  return {
      "path": path,
      "sha256": hashlib.sha256(data).hexdigest(),
      "baseSha256": hashlib.sha256(original).hexdigest()
      if original is not None else None,
      "mode": 0o755 if source_mode & 0o111 else 0o644,
  }


def write_manifest(bundle: str, paths: list[str]) -> None:
  entries = [entry(path) for path in paths]
  payload_hash = hashlib.sha256()
  for value in entries:
    payload_hash.update(json.dumps({
        "baseSha256": value["baseSha256"],
        "mode": value["mode"],
        "path": value["path"],
        "sha256": value["sha256"],
    }, separators=(",", ":"), sort_keys=True).encode())
    payload_hash.update(b"\n")
  manifest = {
      "schemaVersion": 1,
      "id": bundle,
      "baseRevision": BASE_REVISION,
      "requires": ["privacy"] if bundle == "automation" else [],
      "product": bundle,
      "payloadSha256": payload_hash.hexdigest(),
      "files": entries,
  }
  output = SOURCE_DIR / "patches" / bundle / "manifest.json"
  output.parent.mkdir(parents=True, exist_ok=True)
  output.write_text(
      json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
  grouped = {"privacy": [], "automation": []}
  for path in changed_paths():
    grouped[bundle_for(path)].append(path)
  for bundle, paths in grouped.items():
    write_manifest(bundle, paths)
    print(f"{bundle}: {len(paths)} files")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
