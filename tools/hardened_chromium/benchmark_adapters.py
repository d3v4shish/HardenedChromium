#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Deterministically benchmark local adapter verification and dispatch."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from hardened_adapter_pack import load_adapter_pack
from hardened_scrape_broker import schema_collect_items_js


URLS = (
    "https://x.com/home",
    "https://www.linkedin.com/feed/",
    "https://www.facebook.com/groups/test/",
    "https://www.reddit.com/r/test/comments/fixture/thread/",
    "https://web.whatsapp.com/",
    "https://example.test/",
)


def rate(iterations: int, function: object) -> tuple[float, float]:
  samples = []
  for _ in range(5):
    started = time.perf_counter()
    for index in range(iterations):
      function(index)  # type: ignore[operator]
    elapsed = time.perf_counter() - started
    samples.append(iterations / elapsed)
  return statistics.median(samples), max(samples)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--iterations", type=int, default=20000)
  parser.add_argument("--require-gates", action="store_true")
  args = parser.parse_args()
  if args.iterations < 1:
    parser.error("--iterations must be positive")

  started = time.perf_counter()
  pack = load_adapter_pack()
  load_ms = (time.perf_counter() - started) * 1000
  resolve_median, resolve_best = rate(
      args.iterations,
      lambda index: pack.for_url(URLS[index % len(URLS)]))
  schemas = [adapter.schema for adapter in pack.adapters]
  compile_median, compile_best = rate(
      max(1, args.iterations // 10),
      lambda index: schema_collect_items_js(schemas[index % len(schemas)]))

  gates = {
      "loadMsMaximum": 100.0,
      "resolvePerSecondMinimum": 20000.0,
      "schemaCompilePerSecondMinimum": 1000.0,
  }
  passed = (
      load_ms <= gates["loadMsMaximum"] and
      resolve_median >= gates["resolvePerSecondMinimum"] and
      compile_median >= gates["schemaCompilePerSecondMinimum"])
  result = {
      "schemaVersion": 1,
      "adapterPack": pack.id,
      "adapterPackVersion": pack.version,
      "iterations": args.iterations,
      "adapterCount": len(pack.adapters),
      "loadMs": load_ms,
      "resolvePerSecondMedian": resolve_median,
      "resolvePerSecondBest": resolve_best,
      "schemaCompilePerSecondMedian": compile_median,
      "schemaCompilePerSecondBest": compile_best,
      "gates": gates,
      "passed": passed,
  }
  print(json.dumps(result, indent=2, sort_keys=True))
  return 0 if passed or not args.require_gates else 1


if __name__ == "__main__":
  raise SystemExit(main())
