#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Validate Hardened Chromium build identity and isolate profile roles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


PRODUCTS = frozenset({"privacy", "automation"})
PRODUCT_CONTRACTS = {
    "privacy": {"boundary": "red", "remoteDebugging": False,
                "adapterPackEligible": False},
    "automation": {"boundary": "blue", "remoteDebugging": True,
                   "adapterPackEligible": True},
}
BUILD_MANIFEST = "hardened-build-manifest.json"
PROFILE_MARKER = ".hardened-profile.json"


class ProductError(ValueError):
  """Raised when a binary or profile violates its product contract."""


def _product(value: object) -> str:
  product = str(value).strip().lower()
  if product not in PRODUCTS:
    raise ProductError(f"unknown Hardened Chromium product: {value!r}")
  return product


def _read_json(path: Path) -> dict[str, Any]:
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError) as error:
    raise ProductError(f"cannot read {path}: {error}") from error
  if not isinstance(value, dict):
    raise ProductError(f"expected a JSON object in {path}")
  return value


def verify_binary_product(binary: Path, expected_product: str,
                          *, allow_unverified: bool = False) -> dict[str, Any]:
  """Verify the adjacent build manifest and optional binary checksum."""
  expected_product = _product(expected_product)
  binary = binary.expanduser().resolve()
  if not binary.is_file() or not os.access(binary, os.X_OK):
    raise ProductError(f"Hardened Chromium binary is not executable: {binary}")

  manifest_path = binary.parent / BUILD_MANIFEST
  if not manifest_path.is_file():
    if allow_unverified:
      return {"product": expected_product, "unverified": True}
    raise ProductError(
        f"build identity manifest is missing: {manifest_path}; configure the "
        "product build or set HARDENED_ALLOW_UNVERIFIED_BINARY=1 for local "
        "development")

  manifest = _read_json(manifest_path)
  if manifest.get("schemaVersion") != 1:
    raise ProductError(f"unsupported build manifest schema in {manifest_path}")
  actual_product = _product(manifest.get("product"))
  if actual_product != expected_product:
    raise ProductError(
        f"refusing to launch {actual_product} binary as {expected_product}")
  if manifest.get("built") is not True:
    raise ProductError(f"build is not marked complete in {manifest_path}")
  for field, expected in PRODUCT_CONTRACTS[expected_product].items():
    if manifest.get(field) != expected:
      raise ProductError(
          f"{field} does not match the {expected_product} product contract")

  expected_digest = manifest.get("binarySha256")
  if not isinstance(expected_digest, str) or len(expected_digest) != 64:
    raise ProductError(f"invalid binarySha256 in {manifest_path}")
  digest = hashlib.sha256()
  with binary.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(chunk)
  if digest.hexdigest() != expected_digest.lower():
    raise ProductError(f"binary checksum does not match {manifest_path}")
  return manifest


def claim_profile(profile: Path, product: str,
                  *, allow_sharing: bool = False) -> dict[str, Any]:
  """Create or verify an immutable role marker for a user-data directory."""
  product = _product(product)
  profile = profile.expanduser().resolve()
  profile.mkdir(mode=0o700, parents=True, exist_ok=True)
  try:
    profile.chmod(0o700)
  except OSError:
    pass

  marker_path = profile / PROFILE_MARKER
  if marker_path.is_symlink():
    raise ProductError(f"profile marker must not be a symlink: {marker_path}")
  if marker_path.exists():
    marker = _read_json(marker_path)
    if marker.get("schemaVersion") != 1:
      raise ProductError(f"unsupported profile marker schema in {marker_path}")
    actual_product = _product(marker.get("product"))
    if actual_product != product and not allow_sharing:
      raise ProductError(
          f"profile {profile} belongs to {actual_product}; explicit profile "
          "sharing is required")
    return {
        **marker,
        "sharedOverride": actual_product != product,
    }

  marker = {
      "schemaVersion": 1,
      "product": product,
  }
  encoded = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode()
  try:
    descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600)
  except FileExistsError:
    return claim_profile(profile, product, allow_sharing=allow_sharing)
  with os.fdopen(descriptor, "wb") as stream:
    stream.write(encoded)
  return {**marker, "sharedOverride": False}


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--product", required=True, choices=sorted(PRODUCTS))
  parser.add_argument("--binary", type=Path, required=True)
  parser.add_argument("--profile", type=Path, required=True)
  parser.add_argument("--allow-profile-sharing", action="store_true")
  parser.add_argument("--allow-unverified-binary", action="store_true")
  args = parser.parse_args()
  try:
    build = verify_binary_product(
        args.binary, args.product,
        allow_unverified=args.allow_unverified_binary)
    profile = claim_profile(
        args.profile, args.product, allow_sharing=args.allow_profile_sharing)
  except ProductError as error:
    print(error, file=sys.stderr)
    return 2
  print(json.dumps({"build": build, "profile": profile}, sort_keys=True))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
