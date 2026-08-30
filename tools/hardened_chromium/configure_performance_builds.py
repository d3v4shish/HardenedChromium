#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Configure and optionally compile portable and Zen 4 Hardened Chromium."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = SCRIPT_DIR.parent.parent
GN = SOURCE_DIR / "buildtools/linux64/gn"
NINJA = SOURCE_DIR / "third_party/ninja/ninja"
PGO_TOOL = SOURCE_DIR / "tools/update_pgo_profiles.py"
PGO_PROFILE_DIR = SOURCE_DIR / "chrome/build/pgo_profiles"
V8_PGO_TOOL = SOURCE_DIR / "v8/tools/builtins-pgo/download_profiles.py"
V8_PGO_PROFILE = SOURCE_DIR / "v8/tools/builtins-pgo/profiles/x64.profile"
DEPOT_TOOLS = SOURCE_DIR / "third_party/depot_tools"
PGO_GS_URL = "chromium-optimization-profiles/pgo_profiles"
VARIANTS = {
    "portable": (SOURCE_DIR / "out/HardenedPortable", "portable"),
    "zen4": (SOURCE_DIR / "out/HardenedZen4", "znver4"),
}


def run(command: list[str], *, capture: bool = False) -> str:
  print("+", shlex.join(command), flush=True)
  environment = os.environ.copy()
  environment["PATH"] = str(DEPOT_TOOLS) + os.pathsep + environment.get("PATH", "")
  completed = subprocess.run(
      command,
      cwd=SOURCE_DIR,
      check=True,
      env=environment,
      text=True,
      stdout=subprocess.PIPE if capture else None,
  )
  return completed.stdout.strip() if capture else ""


def pgo_profile(fetch: bool) -> Path:
  command = [sys.executable, str(PGO_TOOL), "--target=linux"]
  if fetch:
    PGO_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    run(command + ["update", f"--gs-url-base={PGO_GS_URL}"])
    run([
        sys.executable,
        str(V8_PGO_TOOL),
        "download",
        "--depot-tools",
        str(DEPOT_TOOLS),
        "--check-v8-revision",
        "--quiet",
    ])
  try:
    raw = run(command + ["get_profile_path"], capture=True)
  except subprocess.CalledProcessError as error:
    raise SystemExit(
        "The pinned Linux PGO profile is missing. Re-run with --fetch-pgo "
        "while network access is available; the existing out/Hardened build "
        "has not been changed.") from error
  value = json.loads(raw)
  profile = Path(value)
  if not profile.is_file():
    raise SystemExit(f"PGO profile does not exist: {profile}")
  if not V8_PGO_PROFILE.is_file():
    raise SystemExit(
        "The pinned V8 builtins PGO profile is missing. Re-run with "
        "--fetch-pgo while network access is available.")
  return profile


def gn_args(profile: Path, tuning: str) -> str:
  values = {
      "is_debug": False,
      "is_component_build": False,
      "is_official_build": True,
      "symbol_level": 0,
      "dcheck_always_on": False,
      "enable_expensive_dchecks": False,
      "chrome_pgo_phase": 2,
      "pgo_data_path": str(profile),
      "use_thin_lto": True,
      "is_cfi": True,
      "proprietary_codecs": False,
      "ffmpeg_branding": "Chromium",
      "hardened_chromium_cpu_tuning": tuning,
  }
  lines = []
  for key, value in values.items():
    if isinstance(value, bool):
      encoded = "true" if value else "false"
    else:
      encoded = json.dumps(value)
    lines.append(f"{key} = {encoded}")
  return "\n".join(lines)


def configure(variant: str, profile: Path) -> Path:
  output_dir, tuning = VARIANTS[variant]
  run([
      str(GN), "gen", str(output_dir),
      f"--args={gn_args(profile, tuning)}",
  ])
  return output_dir


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--variant", choices=("portable", "zen4", "both"), default="both")
  parser.add_argument("--fetch-pgo", action="store_true")
  parser.add_argument("--build", action="store_true",
                      help="Compile chrome after generating each build.")
  parser.add_argument("--jobs", type=int, default=0,
                      help="Ninja parallelism; zero uses Ninja's default.")
  args = parser.parse_args()

  profile = pgo_profile(args.fetch_pgo)
  variants = VARIANTS if args.variant == "both" else (args.variant,)
  for variant in variants:
    output_dir = configure(variant, profile)
    if args.build:
      command = [str(NINJA), "-C", str(output_dir)]
      if args.jobs > 0:
        command += ["-j", str(args.jobs)]
      run(command + ["chrome"])
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
