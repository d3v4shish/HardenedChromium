#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Load and verify the local, read-only Hardened Chromium adapter pack."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PACK_DIR = SCRIPT_DIR / "adapter_packs/default"
CRAWL_MODES = frozenset({"current", "scope", "targets", "account"})


class AdapterError(ValueError):
  """Raised when a pack or adapter request violates its contract."""


@dataclasses.dataclass(frozen=True)
class Adapter:
  id: str
  name: str
  version: str
  domains: tuple[str, ...]
  views: tuple[str, ...]
  modes: tuple[str, ...]
  schema: dict[str, Any]
  advance_selector: str
  advance_direction: str
  snapshot_selector: str
  account_target_selector: str
  safety: tuple[str, ...]
  schema_sha256: str

  def summary(self) -> dict[str, Any]:
    return {
        "id": self.id,
        "name": self.name,
        "version": self.version,
        "domains": list(self.domains),
        "views": list(self.views),
        "crawlModes": list(self.modes),
        "defaultCrawlMode": "scope",
        "safety": list(self.safety),
        "schemaSha256": self.schema_sha256,
    }


@dataclasses.dataclass(frozen=True)
class AdapterPack:
  id: str
  version: str
  path: Path
  adapters: tuple[Adapter, ...]

  def summary(self) -> dict[str, Any]:
    return {
        "id": self.id,
        "version": self.version,
        "local": True,
        "verified": True,
        "adapters": [adapter.summary() for adapter in self.adapters],
    }

  def get(self, adapter_id: str) -> Adapter | None:
    adapter_id = str(adapter_id).strip().lower()
    return next((value for value in self.adapters if value.id == adapter_id), None)

  def for_url(self, url: str) -> Adapter | None:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for adapter in self.adapters:
      if any(host == domain or host.endswith(f".{domain}")
             for domain in adapter.domains):
        return adapter
    return None

  def resolve(self, requested: object, url: str) -> Adapter | None:
    value = str(requested or "auto").strip().lower()
    detected = self.for_url(url)
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    is_whatsapp = (host == "web.whatsapp.com" or
                   host.endswith(".web.whatsapp.com"))
    if is_whatsapp and (not detected or detected.id != "whatsapp"):
      raise AdapterError(
          "WhatsApp collection requires the selected-conversation adapter")
    if value in ("", "auto"):
      return detected
    if value in ("none", "generic"):
      if is_whatsapp:
        raise AdapterError(
            "WhatsApp collection cannot bypass the selected-conversation adapter")
      return None
    adapter = self.get(value)
    if not adapter:
      raise AdapterError(f"unknown adapter: {value}")
    if not detected or detected.id != adapter.id:
      raise AdapterError(
          f"adapter {adapter.id} does not allow the requested URL host")
    return adapter


def _string_tuple(raw: object, field: str) -> tuple[str, ...]:
  if not isinstance(raw, list) or not raw:
    raise AdapterError(f"adapter {field} must be a non-empty list")
  values = tuple(str(value).strip().lower() for value in raw if str(value).strip())
  if not values:
    raise AdapterError(f"adapter {field} must be a non-empty list")
  return values


def _selectors_are_scoped(value: object, root: str) -> bool:
  selectors = [selector.strip() for selector in str(value or "").split(",")]
  return bool(selectors) and all(
      selector == root or selector.startswith(f"{root} ")
      for selector in selectors)


def _validate_domains(
    adapter_id: str,
    domains: tuple[str, ...],
  claimed_domains: dict[str, str],
) -> None:
  for domain in domains:
    labels = domain.split(".")
    if (len(domain) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels)):
      raise AdapterError(f"adapter {adapter_id} has invalid domain {domain!r}")
    for claimed, claimed_by in claimed_domains.items():
      if (domain == claimed or domain.endswith(f".{claimed}") or
          claimed.endswith(f".{domain}")):
        raise AdapterError(
            f"adapter domain {domain} overlaps {claimed_by}:{claimed}")
    claimed_domains[domain] = adapter_id


def load_adapter_pack(path: Path = DEFAULT_PACK_DIR) -> AdapterPack:
  """Load a pack only after every referenced schema matches its checksum."""
  path = path.resolve()
  manifest_path = path / "manifest.json"
  try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
  except (OSError, ValueError) as error:
    raise AdapterError(f"cannot load adapter manifest {manifest_path}: {error}") from error
  if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
    raise AdapterError("adapter manifest schemaVersion must be 1")
  raw_adapters = manifest.get("adapters")
  if not isinstance(raw_adapters, list) or not raw_adapters:
    raise AdapterError("adapter manifest must contain adapters")

  adapters: list[Adapter] = []
  used_ids: set[str] = set()
  claimed_domains: dict[str, str] = {}
  for raw in raw_adapters:
    if not isinstance(raw, dict):
      raise AdapterError("adapter manifest entries must be objects")
    adapter_id = str(raw.get("id") or "").strip().lower()
    if not adapter_id or adapter_id in used_ids:
      raise AdapterError(f"invalid or duplicate adapter id: {adapter_id!r}")
    used_ids.add(adapter_id)
    schema_name = str(raw.get("schemaFile") or "")
    schema_path = (path / schema_name).resolve()
    if schema_path.parent != path or not schema_name.endswith(".json"):
      raise AdapterError(f"invalid schemaFile for adapter {adapter_id}")
    try:
      schema_bytes = schema_path.read_bytes()
    except OSError as error:
      raise AdapterError(f"cannot read adapter schema {schema_path}: {error}") from error
    actual_sha256 = hashlib.sha256(schema_bytes).hexdigest()
    expected_sha256 = str(raw.get("schemaSha256") or "").lower()
    if actual_sha256 != expected_sha256:
      raise AdapterError(f"checksum mismatch for adapter {adapter_id}")
    try:
      schema = json.loads(schema_bytes)
    except ValueError as error:
      raise AdapterError(f"invalid schema for adapter {adapter_id}: {error}") from error
    if not isinstance(schema, dict):
      raise AdapterError(f"adapter schema {adapter_id} must be an object")
    if not str(schema.get("itemRoot") or "").strip():
      raise AdapterError(f"adapter schema {adapter_id} requires itemRoot")
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
      raise AdapterError(f"adapter schema {adapter_id} requires fields")
    modes = _string_tuple(raw.get("crawlModes"), "crawlModes")
    if any(mode not in CRAWL_MODES for mode in modes):
      raise AdapterError(f"adapter {adapter_id} declares an unknown crawl mode")
    advance_direction = str(raw.get("advanceDirection") or "down").lower()
    if advance_direction not in ("down", "up"):
      raise AdapterError(f"adapter {adapter_id} has invalid advanceDirection")
    domains = _string_tuple(raw.get("domains"), "domains")
    _validate_domains(adapter_id, domains, claimed_domains)
    adapter = Adapter(
        id=adapter_id,
        name=str(raw.get("name") or adapter_id),
        version=str(raw.get("version") or manifest.get("version") or "1"),
        domains=domains,
        views=_string_tuple(raw.get("views"), "views"),
        modes=modes,
        schema=schema,
        advance_selector=str(raw.get("advanceSelector") or ""),
        advance_direction=advance_direction,
        snapshot_selector=str(raw.get("snapshotSelector") or ""),
        account_target_selector=str(raw.get("accountTargetSelector") or ""),
        safety=tuple(str(value) for value in raw.get("safety", [])
                     if str(value).strip()),
        schema_sha256=actual_sha256,
    )
    if adapter.id == "whatsapp":
      conversation_scope = str(adapter.schema.get("scopeRoot") or "")
      if (adapter.domains != ("web.whatsapp.com",) or
          set(adapter.modes) - {"current", "scope"} or
          adapter.snapshot_selector != conversation_scope or
          "conversation-panel-messages" not in conversation_scope or
          not _selectors_are_scoped(conversation_scope, "#main") or
          not _selectors_are_scoped(adapter.schema.get("itemRoot"), "#main") or
          not _selectors_are_scoped(adapter.advance_selector, "#main") or
          adapter.account_target_selector):
        raise AdapterError(
            "WhatsApp adapter must remain scoped to the selected conversation")
    adapters.append(adapter)
  return AdapterPack(
      id=str(manifest.get("id") or "default"),
      version=str(manifest.get("version") or "1"),
      path=path,
      adapters=tuple(adapters))


def validate_crawl_request(request: dict[str, Any],
                           adapter: Adapter | None) -> tuple[str, list[str]]:
  mode = str(request.get("crawl_mode", request.get("crawlMode", "scope"))).lower()
  if mode not in CRAWL_MODES:
    raise AdapterError("crawlMode must be current, scope, targets, or account")
  if adapter and mode not in adapter.modes:
    raise AdapterError(f"adapter {adapter.id} does not support crawl mode {mode}")
  account_confirmation = request.get("confirm_account", request.get(
      "confirmAccount", request.get("account_confirmed", False)))
  if mode == "account" and account_confirmation is not True:
    raise AdapterError("account crawl requires confirmAccount=true")
  raw_targets = request.get("targets", request.get("crawl_targets", []))
  if raw_targets is None:
    raw_targets = []
  if not isinstance(raw_targets, list):
    raise AdapterError("targets must be a list")
  if len(raw_targets) > 1000:
    raise AdapterError("targets may contain at most 1000 entries")
  targets = list(dict.fromkeys(
      str(value).strip() for value in raw_targets if str(value).strip()))
  if mode == "targets" and not targets:
    raise AdapterError("targets crawl requires at least one target")
  if adapter and adapter.id == "whatsapp" and mode not in ("current", "scope"):
    raise AdapterError(
        "WhatsApp is restricted to the currently selected conversation")
  return mode, targets
