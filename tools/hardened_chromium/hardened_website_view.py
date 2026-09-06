#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Profile-scoped policy for values exposed to websites.

The browser consumes the supported parts of this JSON document through
``--hardened-privacy-rules-file``. Keeping the format and resolver here lets
the local broker, the Settings WebUI, and tests agree on the exact-origin
policy without giving applications access to CDP. Unsupported expert fields
are retained and reported as warnings rather than silently changed.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = 3
SOURCE_VALUES = frozenset(("fake", "real"))
EXPOSURE_VALUES = {
    "automation": frozenset(("hide", "report")),
    "canvas": frozenset(("block", "normalize", "actual", "custom")),
    "webgl": frozenset(("block", "normalize", "actual", "custom")),
    "audio": frozenset(("block", "normalize", "actual", "custom")),
    "webRtc": frozenset(("block", "normalize", "actual")),
    "localFonts": frozenset(("block", "normalize", "actual")),
    "highEntropyApis": frozenset(("block", "normalize", "actual")),
    "deviceEnumeration": frozenset(("block", "normalize", "actual")),
    "behavior": frozenset(("hardened", "stock")),
}

DEFAULT_DOCUMENT: dict[str, Any] = {
    "schemaVersion": SCHEMA_VERSION,
    "default": {
        "cameraSource": "fake",
        "microphoneSource": "fake",
        "locationSource": "fake",
        "persona": {
            # Empty fields preserve Chromium's ordinary profile value. They are
            # intentionally editable rather than replaced with a fork-specific
            # identity.
            "userAgent": "",
            "platform": "",
            "languages": [],
            "timezone": "",
            "hardwareConcurrency": None,
            "deviceMemory": None,
            "maxTouchPoints": None,
            "screen": {},
            # Advanced fields are intentionally retained verbatim. Rendering
            # surfaces use modes above until a browser-side implementation can
            # safely consume a specific custom value.
            "custom": {},
        },
        "exposures": {
            "automation": "hide",
            "canvas": "normalize",
            "webgl": "normalize",
            "audio": "normalize",
            "webRtc": "block",
            "localFonts": "block",
            "highEntropyApis": "block",
            "deviceEnumeration": "normalize",
            "behavior": "hardened",
        },
    },
    "rules": [],
}


def normalize_origin(value: Any) -> str:
  """Return an exact http(s) origin, or an empty string for invalid input."""
  text = str(value or "").strip()
  try:
    parsed = urlparse(text)
  except ValueError:
    return ""
  if parsed.scheme not in ("http", "https") or not parsed.hostname:
    return ""
  if parsed.username or parsed.password or parsed.path not in ("", "/"):
    return ""
  if parsed.query or parsed.fragment:
    return ""
  hostname = parsed.hostname.lower()
  try:
    port = parsed.port
  except ValueError:
    return ""
  default_port = 443 if parsed.scheme == "https" else 80
  port_suffix = "" if port in (None, default_port) else f":{port}"
  return f"{parsed.scheme}://{hostname}{port_suffix}"


def origin_for_url(value: Any) -> str:
  """Return the exact origin for an http(s) URL or origin."""
  text = str(value or "").strip()
  try:
    parsed = urlparse(text)
  except ValueError:
    return ""
  if parsed.scheme not in ("http", "https") or not parsed.hostname:
    return ""
  if parsed.username or parsed.password:
    return ""
  hostname = parsed.hostname.lower()
  try:
    port = parsed.port
  except ValueError:
    return ""
  default_port = 443 if parsed.scheme == "https" else 80
  port_suffix = "" if port in (None, default_port) else f":{port}"
  return f"{parsed.scheme}://{hostname}{port_suffix}"


def _source(value: Any, fallback: Any) -> Any:
  """Keep an expert value intact; the browser will fail closed if unknown."""
  return copy.deepcopy(fallback if value is None else value)


def _exposure(_name: str, value: Any, fallback: Any) -> Any:
  """Do not turn an unknown mode into an apparently valid configuration."""
  return copy.deepcopy(fallback if value is None else value)


def _json_object(value: Any) -> dict[str, Any]:
  return copy.deepcopy(value) if isinstance(value, dict) else {}


def _persona(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
  supplied = _json_object(value)
  result = copy.deepcopy(fallback)
  # `custom` is the documented extension point, but retain every additional
  # JSON field too. That allows an expert to prepare a future browser field
  # without a broker update discarding it.
  result.update(copy.deepcopy(supplied))
  return result


def _policy(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
  supplied = _json_object(value)
  result = copy.deepcopy(fallback)
  for name in ("cameraSource", "microphoneSource", "locationSource"):
    result[name] = _source(supplied.get(name), result[name])
  result["persona"] = _persona(supplied.get("persona"), result["persona"])
  exposures = _json_object(supplied.get("exposures"))
  for name in EXPOSURE_VALUES:
    result["exposures"][name] = _exposure(
        name, exposures.get(name), result["exposures"][name])
  for name, expert_value in exposures.items():
    if name not in EXPOSURE_VALUES:
      result["exposures"][name] = copy.deepcopy(expert_value)
  for name, expert_value in supplied.items():
    if name not in {
        "cameraSource", "microphoneSource", "locationSource", "persona",
        "exposures",
    }:
      result[name] = copy.deepcopy(expert_value)
  return result


def normalize_document(value: Any) -> dict[str, Any]:
  """Normalize untrusted persisted input without inventing an identity."""
  source = _json_object(value)
  default = _policy(source.get("default"), DEFAULT_DOCUMENT["default"])
  rules: list[dict[str, Any]] = []
  raw_rules = source.get("rules")
  if isinstance(raw_rules, list):
    seen: set[str] = set()
    for raw_rule in raw_rules:
      rule = _json_object(raw_rule)
      origin = normalize_origin(rule.get("origin"))
      if not origin or origin in seen:
        continue
      seen.add(origin)
      policy = _policy(rule, default)
      entry = copy.deepcopy(rule)
      entry.update({
          "id": rule.get("id") if isinstance(rule.get("id"), str) else origin,
          "origin": origin,
          "cameraSource": policy["cameraSource"],
          "microphoneSource": policy["microphoneSource"],
          "locationSource": policy["locationSource"],
          "persona": policy["persona"],
          "exposures": policy["exposures"],
      })
      rules.append(entry)
  rules.sort(key=lambda rule: rule["origin"])
  return {"schemaVersion": SCHEMA_VERSION, "default": default, "rules": rules}


def load_document(path: Path) -> dict[str, Any]:
  try:
    with path.open("r", encoding="utf-8") as source:
      return normalize_document(json.load(source))
  except (OSError, ValueError, TypeError):
    return copy.deepcopy(DEFAULT_DOCUMENT)


def atomic_write_document(path: Path, document: dict[str, Any]) -> dict[str, Any]:
  normalized = normalize_document(document)
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(
      prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
  try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
      json.dump(normalized, destination, indent=2, sort_keys=True)
      destination.write("\n")
      destination.flush()
      os.fsync(destination.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, path)
  finally:
    try:
      os.unlink(temporary_name)
    except FileNotFoundError:
      pass
  return normalized


def upsert_rule(document: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
  """Return a normalized document with one exact-origin override replaced."""
  normalized = normalize_document(document)
  origin = normalize_origin(rule.get("origin"))
  if not origin:
    raise ValueError("origin must be an exact http(s) origin")
  replacement = _policy(rule, normalized["default"])
  entry = copy.deepcopy(rule)
  entry.update({
      "id": str(rule.get("id") or origin),
      "origin": origin,
      "cameraSource": replacement["cameraSource"],
      "microphoneSource": replacement["microphoneSource"],
      "locationSource": replacement["locationSource"],
      "persona": replacement["persona"],
      "exposures": replacement["exposures"],
  })
  normalized["rules"] = [
      existing for existing in normalized["rules"]
      if existing["origin"] != origin
  ]
  normalized["rules"].append(entry)
  normalized["rules"].sort(key=lambda existing: existing["origin"])
  return normalized


def resolve_policy(document: dict[str, Any], origin: Any) -> dict[str, Any]:
  """Resolve only an exact-origin rule; subdomains and frames do not inherit."""
  normalized = normalize_document(document)
  wanted_origin = origin_for_url(origin)
  for rule in normalized["rules"]:
    if rule["origin"] == wanted_origin:
      return _policy(rule, normalized["default"])
  return copy.deepcopy(normalized["default"])


def warnings_for_policy(policy: dict[str, Any]) -> list[str]:
  """Return warnings, never silent substitutions, for expert configuration."""
  warnings: list[str] = []
  persona = policy.get("persona", {})
  for name in ("cameraSource", "microphoneSource", "locationSource"):
    if policy.get(name) not in SOURCE_VALUES:
      warnings.append(
          f"{name} is not supported and will fall back to the browser's "
          "privacy-preserving source.")
  exposures = policy.get("exposures", {})
  if not isinstance(exposures, dict):
    warnings.append("Exposure modes must be a JSON object.")
    exposures = {}
  for name, allowed_values in EXPOSURE_VALUES.items():
    if exposures.get(name) not in allowed_values:
      warnings.append(
          f"{name}={exposures.get(name)!r} is retained but is not implemented "
          "by this browser build.")
    elif name != "automation":
      warnings.append(
          f"{name} is retained, but this browser build does not yet enforce "
          "that exposure mode.")
  if not isinstance(persona, dict):
    warnings.append("Persona must be a JSON object.")
    persona = {}
  if bool(persona.get("userAgent")) != bool(persona.get("platform")):
    warnings.append("User-Agent and platform should be set together.")
  if persona.get("timezone") and not persona.get("languages"):
    warnings.append("Timezone without languages can be a distinctive combination.")
  if any(value not in (None, "", [], {}) for value in persona.values()):
    warnings.append(
        "Persona fields are retained but are not yet applied by this browser "
        "build.")
  if exposures.get("webRtc") == "actual":
    warnings.append("Actual WebRTC can reveal local-network characteristics.")
  if exposures.get("automation") == "report":
    warnings.append("Reporting automation exposes navigator.webdriver.")
  return warnings
