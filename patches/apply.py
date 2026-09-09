#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Apply a checked Hardened Chromium overlay bundle to a pinned checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = SCRIPT_DIR.parent
STATE_DIR = ".hardened-patches"


class PatchError(RuntimeError):
  pass


def digest(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


def manifest_payload_digest(raw_files: list[dict[str, Any]]) -> str:
  payload_hash = hashlib.sha256()
  seen: set[str] = set()
  for raw in raw_files:
    if not isinstance(raw, dict):
      raise PatchError("bundle file entry must be an object")
    path = raw.get("path")
    checksum = raw.get("sha256")
    if not isinstance(path, str) or not isinstance(checksum, str):
      raise PatchError("bundle file entry is missing path or checksum")
    checksum = checksum.lower()
    if len(checksum) != 64 or any(
        character not in "0123456789abcdef" for character in checksum):
      raise PatchError(f"bundle file entry has invalid checksum: {path}")
    base_checksum = raw.get("baseSha256")
    if base_checksum is not None:
      if not isinstance(base_checksum, str):
        raise PatchError(f"bundle file entry has invalid base checksum: {path}")
      base_checksum = base_checksum.lower()
      if len(base_checksum) != 64 or any(
          character not in "0123456789abcdef" for character in base_checksum):
        raise PatchError(f"bundle file entry has invalid base checksum: {path}")
    mode = raw.get("mode")
    if (isinstance(mode, bool) or not isinstance(mode, int) or
        mode < 0 or mode > 0o777):
      raise PatchError(f"bundle file entry has invalid mode: {path}")
    if path in seen:
      raise PatchError(f"bundle contains duplicate path: {path}")
    seen.add(path)
    payload_hash.update(json.dumps({
        "baseSha256": base_checksum,
        "mode": mode,
        "path": path,
        "sha256": checksum,
    }, separators=(",", ":"), sort_keys=True).encode())
    payload_hash.update(b"\n")
  return payload_hash.hexdigest()


def read_manifest(bundle: str) -> tuple[Path, dict[str, Any]]:
  path = SCRIPT_DIR / bundle / "manifest.json"
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError) as error:
    raise PatchError(f"cannot read bundle manifest {path}: {error}") from error
  if not isinstance(value, dict) or value.get("schemaVersion") != 1:
    raise PatchError(f"unsupported bundle manifest: {path}")
  if value.get("id") != bundle:
    raise PatchError(f"bundle id does not match directory: {path}")
  return path, value


def git_revision(target: Path) -> str:
  try:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=target, check=True,
        text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE).stdout.strip()
  except subprocess.CalledProcessError as error:
    raise PatchError(f"target is not a Git checkout: {target}") from error


def verify_entry(target: Path, raw: dict[str, Any]) -> tuple[Path, Path, int]:
  relative = Path(str(raw.get("path") or ""))
  if (not relative.parts or relative.is_absolute() or ".." in relative.parts or
      relative.parts[0] in (".git", STATE_DIR)):
    raise PatchError(f"invalid bundle path: {relative}")
  source = (SOURCE_DIR / relative).resolve()
  destination = target / relative
  if not source.is_file() or not source.is_relative_to(SOURCE_DIR):
    raise PatchError(f"bundle payload is missing: {relative}")
  cursor = target
  for part in relative.parts:
    cursor /= part
    if cursor.is_symlink():
      raise PatchError(f"bundle destination must not be a symlink: {relative}")
  source_bytes = source.read_bytes()
  if digest(source_bytes) != raw.get("sha256"):
    raise PatchError(f"bundle payload checksum mismatch: {relative}")
  expected_base = raw.get("baseSha256")
  expected_mode = int(raw["mode"])
  if destination.exists():
    current = digest(destination.read_bytes())
    if current == raw.get("sha256"):
      actual_mode = stat.S_IMODE(destination.stat().st_mode)
      if actual_mode != expected_mode:
        raise PatchError(
            f"target file has unexpected mode: {relative} "
            f"({actual_mode:o}, expected {expected_mode:o})")
      return source, destination, expected_mode
    if expected_base is None or current != expected_base:
      raise PatchError(f"target file has unexpected content: {relative}")
  elif expected_base is not None:
    raise PatchError(f"target file is unexpectedly missing: {relative}")
  return source, destination, expected_mode


def verify_dependency(target: Path, dependency: str, state_dir: Path,
                      expected_revision: str) -> None:
  state_path = state_dir / f"{dependency}.json"
  if not state_path.is_file() or state_path.is_symlink():
    raise PatchError(f"bundle requires applied bundle {dependency}")
  try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
  except (OSError, ValueError) as error:
    raise PatchError(f"invalid dependency state {state_path}: {error}") from error
  _path, manifest = read_manifest(dependency)
  if (not isinstance(state, dict) or state.get("schemaVersion") != 1 or
      state.get("bundle") != dependency or
      state.get("baseRevision") != expected_revision or
      state.get("payloadSha256") != manifest.get("payloadSha256")):
    raise PatchError(f"dependency state does not match bundle {dependency}")
  raw_files = manifest.get("files")
  if (not isinstance(raw_files, list) or
      manifest_payload_digest(raw_files) != manifest.get("payloadSha256")):
    raise PatchError(f"dependency manifest is invalid: {dependency}")
  for raw in raw_files:
    _source, destination, _mode = verify_entry(target, raw)
    if (not destination.is_file() or
        digest(destination.read_bytes()) != raw["sha256"]):
      raise PatchError(f"dependency file is not applied: {raw['path']}")


def apply_bundle(bundle: str, target: Path, *, check_only: bool = False) -> None:
  _manifest_path, manifest = read_manifest(bundle)
  target = target.expanduser().resolve()
  if target == SOURCE_DIR:
    raise PatchError("source overlay and target checkout must be different")
  expected_revision = str(manifest.get("baseRevision") or "")
  actual_revision = git_revision(target)
  if actual_revision != expected_revision:
    raise PatchError(
        f"target revision mismatch: expected {expected_revision}, got {actual_revision}")

  state_dir = target / STATE_DIR
  if state_dir.is_symlink():
    raise PatchError(f"patch state directory must not be a symlink: {state_dir}")
  if state_dir.exists() and not state_dir.is_dir():
    raise PatchError(f"patch state path must be a directory: {state_dir}")
  state_path = state_dir / f"{bundle}.json"
  if state_path.is_symlink():
    raise PatchError(f"patch state file must not be a symlink: {state_path}")
  dependencies = manifest.get("requires", [])
  if not isinstance(dependencies, list):
    raise PatchError(f"bundle {bundle} has invalid dependencies")
  for dependency in dependencies:
    if dependency not in ("privacy", "automation") or dependency == bundle:
      raise PatchError(f"bundle {bundle} has an invalid dependency")
    verify_dependency(target, dependency, state_dir, expected_revision)

  raw_files = manifest.get("files")
  if not isinstance(raw_files, list) or not raw_files:
    raise PatchError(f"bundle {bundle} has no payload files")
  if manifest_payload_digest(raw_files) != manifest.get("payloadSha256"):
    raise PatchError(f"bundle {bundle} aggregate checksum mismatch")
  verified = [verify_entry(target, raw) for raw in raw_files]
  if check_only:
    print(f"Verified {bundle}: {len(verified)} files")
    return

  for source, destination, mode in verified:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
      with tempfile.NamedTemporaryFile(
          dir=destination.parent, prefix=f".{destination.name}.",
          delete=False) as temporary:
        temporary_path = Path(temporary.name)
        with source.open("rb") as stream:
          shutil.copyfileobj(stream, temporary)
      os.chmod(temporary_path, mode)
      os.replace(temporary_path, destination)
      temporary_path = None
    finally:
      if temporary_path is not None:
        temporary_path.unlink(missing_ok=True)

  state_dir.mkdir(mode=0o700, exist_ok=True)
  state = {
      "schemaVersion": 1,
      "bundle": bundle,
      "baseRevision": expected_revision,
      "payloadSha256": manifest.get("payloadSha256"),
  }
  encoded_state = (
      json.dumps(state, indent=2, sort_keys=True) + "\n").encode()
  temporary_state: Path | None = None
  try:
    with tempfile.NamedTemporaryFile(
        dir=state_dir, prefix=f".{bundle}.", delete=False) as temporary:
      temporary_state = Path(temporary.name)
      temporary.write(encoded_state)
    os.chmod(temporary_state, 0o600)
    os.replace(temporary_state, state_path)
    temporary_state = None
  finally:
    if temporary_state is not None:
      temporary_state.unlink(missing_ok=True)
  print(f"Applied {bundle}: {len(verified)} files")


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("bundle", choices=("privacy", "automation"))
  parser.add_argument("--target", type=Path, required=True)
  parser.add_argument("--check", action="store_true",
                      help="Verify without modifying the target checkout.")
  args = parser.parse_args()
  try:
    apply_bundle(args.bundle, args.target, check_only=args.check)
  except PatchError as error:
    print(error, file=sys.stderr)
    return 2
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
