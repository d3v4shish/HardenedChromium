#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Prefer OBS Virtual Camera in a Hardened Chromium profile.

This edits Chromium's JSON Preferences before the browser starts. It does not
grant camera/microphone permission, and it does not enable fake media devices.
It only ranks OBS-like video capture devices first so normal permission prompts
prefer the OBS virtual camera when it is available.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
from typing import Any


VIDEO_RANKING_PATH = ("media", "video_input", "user_preference_ranking")
LEGACY_VIDEO_DEFAULT_PATH = ("media", "default_video_capture_Device")
DEFAULT_CAMERA_NAMES = (
    "OBS Virtual Camera",
    "OBS Camera",
)


def sysfs_video_devices() -> list[tuple[str, str, str]]:
  devices: list[tuple[str, str, str]] = []
  for entry in sorted(Path("/sys/class/video4linux").glob("video*")):
    name = read_text(entry / "name").strip()
    device_node = f"/dev/{entry.name}"
    model_id = usb_model_id(entry)
    if name:
      devices.append((name, device_node, model_id))
  return devices


def v4l2_ctl_device_names() -> list[str]:
  try:
    result = subprocess.run(
        ["v4l2-ctl", "--list-devices"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
  except (OSError, subprocess.TimeoutExpired):
    return []
  names: list[str] = []
  for line in result.stdout.splitlines():
    if not line or line.startswith((" ", "\t")) or not line.endswith(":"):
      continue
    name = line[:-1].strip()
    # v4l2-ctl commonly formats this as "Name (bus-info):". Chromium usually
    # uses the V4L2 card/interface name, so keep the name portion as well.
    names.append(name)
    match = re.match(r"^(.*?)\s+\([^)]*\)$", name)
    if match:
      names.append(match.group(1).strip())
  return names


def usb_model_id(entry: Path) -> str:
  device_dir = entry / "device"
  vendor = read_text(device_dir / "../idVendor").strip()
  product = read_text(device_dir / "../idProduct").strip()
  if len(vendor) >= 4 and len(product) >= 4:
    return f"{vendor[:4]}:{product[:4]}"
  return ""


def read_text(path: Path) -> str:
  try:
    return path.read_text(encoding="utf-8", errors="replace")
  except OSError:
    return ""


def stable_video_id(name: str, model_id: str = "") -> str:
  name = name.strip()
  model_id = model_id.strip()
  if not name:
    return ""
  return f"{name} ({model_id})" if model_id else name


def obs_candidates(camera_names: list[str], match_text: str) -> tuple[list[str], str]:
  candidates: list[str] = []
  default_unique_id = ""
  lower_match = match_text.lower()

  def add(value: str) -> None:
    value = value.strip()
    if value and value not in candidates:
      candidates.append(value)

  for name in camera_names:
    add(name)

  for name, device_node, model_id in sysfs_video_devices():
    stable_id = stable_video_id(name, model_id)
    searchable = f"{name} {stable_id} {device_node}".lower()
    if lower_match in searchable or "obs" in searchable:
      add(stable_id)
      add(name)
      if not default_unique_id:
        default_unique_id = device_node

  for name in v4l2_ctl_device_names():
    if lower_match in name.lower() or "obs" in name.lower():
      add(name)

  return candidates, default_unique_id


def get_nested(root: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
  current: Any = root
  for key in path:
    if not isinstance(current, dict) or key not in current:
      return default
    current = current[key]
  return current


def set_nested(root: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
  current = root
  for key in path[:-1]:
    child = current.get(key)
    if not isinstance(child, dict):
      child = {}
      current[key] = child
    current = child
  current[path[-1]] = value


def load_preferences(path: Path) -> dict[str, Any]:
  if not path.exists():
    return {}
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
      return value
  except json.JSONDecodeError as error:
    raise SystemExit(f"Invalid Chromium Preferences JSON at {path}: {error}") from error
  return {}


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
  temporary.write_text(
      json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
      encoding="utf-8",
  )
  temporary.replace(path)


def configure_preferences(
    user_data_dir: Path,
    profile_directory: str,
    camera_names: list[str],
    match_text: str,
) -> dict[str, Any]:
  preferences_path = user_data_dir / profile_directory / "Preferences"
  prefs = load_preferences(preferences_path)
  candidates, default_unique_id = obs_candidates(camera_names, match_text)

  existing = get_nested(prefs, VIDEO_RANKING_PATH, [])
  if not isinstance(existing, list):
    existing = []
  ranking: list[str] = []
  for value in [*candidates, *existing]:
    if isinstance(value, str) and value and value not in ranking:
      ranking.append(value)
  set_nested(prefs, VIDEO_RANKING_PATH, ranking)
  if default_unique_id:
    set_nested(prefs, LEGACY_VIDEO_DEFAULT_PATH, default_unique_id)

  atomic_write_json(preferences_path, prefs)
  return {
      "preferences": str(preferences_path),
      "ranking": ranking,
      "legacyDefaultDevice": default_unique_id,
  }


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Rank OBS Virtual Camera first in a Chromium profile.")
  parser.add_argument("--user-data-dir", type=Path, required=True)
  parser.add_argument("--profile-directory", default="Default")
  parser.add_argument("--camera-name", action="append", default=[],
                      help="Stable Chromium camera name to prefer. Can repeat.")
  parser.add_argument("--match", default="obs",
                      help="Case-insensitive token used to discover OBS devices.")
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  camera_names = args.camera_name or list(DEFAULT_CAMERA_NAMES)
  result = configure_preferences(
      args.user_data_dir.expanduser(),
      args.profile_directory,
      camera_names,
      args.match,
  )
  print("Configured OBS camera preference:")
  print(f"  Preferences: {result['preferences']}")
  print(f"  Video ranking: {', '.join(result['ranking'][:5])}")
  if result["legacyDefaultDevice"]:
    print(f"  Legacy default device: {result['legacyDefaultDevice']}")
  else:
    print("  OBS device not currently visible; seeded by name for next launch.")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
