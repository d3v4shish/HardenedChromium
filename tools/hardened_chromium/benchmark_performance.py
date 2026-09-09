#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Benchmark Hardened Chromium variants and write the auto-selection manifest."""

from __future__ import annotations

import argparse
import contextlib
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from hardened_scrape_broker import (
    COLLECT_ITEMS_JS,
    CdpWebSocket,
    Job,
    add_items,
    cdp_create_tab,
    evaluate,
    write_outputs,
)


SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = SCRIPT_DIR.parent.parent
DEFAULT_REPORT = SOURCE_DIR / "out/HardenedPerformance/performance-results.json"
SANDBOX_HELPER = Path(os.environ.get(
    "CHROME_DEVEL_SANDBOX", "/usr/local/sbin/chrome-devel-sandbox"))
VARIANT_BINARIES = {
    "portable": SOURCE_DIR / "out/HardenedAutomation/chrome",
    "zen4": SOURCE_DIR / "out/HardenedAutomationZen4/chrome",
}
SPEEDOMETER_PATH = (
    "/third_party/blink/perf_tests/speedometer21/InteractiveRunner.html"
    "?startAutomatically=1")
FIXTURE_PATH = (
    "/tools/hardened_chromium/performance_fixture.html"
    "?items=600")
SCORE_RE = re.compile(r"Score\s*:\s*([0-9.]+)\s+rpm")
ZNVER4_FLAGS = {
    "avx2", "fma", "bmi1", "bmi2", "avx512f", "avx512dq",
    "avx512cd", "avx512bw", "avx512vl", "avx512_bf16", "avx512vbmi",
    "avx512_vbmi2", "avx512_vnni", "avx512_bitalg", "avx512_vpopcntdq",
}


class QuietHandler(SimpleHTTPRequestHandler):
  def log_message(self, _format: str, *_args: Any) -> None:
    pass


class SourceServer:
  def __init__(self) -> None:
    handler = functools.partial(QuietHandler, directory=str(SOURCE_DIR))
    self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

  def __enter__(self) -> "SourceServer":
    self.thread.start()
    return self

  def __exit__(self, *_args: Any) -> None:
    self.server.shutdown()
    self.server.server_close()
    self.thread.join(timeout=3)

  @property
  def origin(self) -> str:
    return f"http://127.0.0.1:{int(self.server.server_address[1])}"


def process_tree(root_pid: int) -> set[int]:
  pending = [root_pid]
  found: set[int] = set()
  while pending:
    pid = pending.pop()
    if pid in found:
      continue
    found.add(pid)
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    with contextlib.suppress(OSError, ValueError):
      pending.extend(int(value) for value in children_path.read_text().split())
  return found


def process_tree_rss_kib(root_pid: int) -> int:
  total = 0
  for pid in process_tree(root_pid):
    with contextlib.suppress(OSError, ValueError):
      for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
          total += int(line.split()[1])
          break
  return total


class RssMonitor:
  def __init__(self, pid: int):
    self.pid = pid
    self.peak_kib = 0
    self.stop_event = threading.Event()
    self.thread = threading.Thread(target=self.run, daemon=True)

  def start(self) -> None:
    self.thread.start()

  def run(self) -> None:
    while not self.stop_event.wait(0.05):
      self.peak_kib = max(self.peak_kib, process_tree_rss_kib(self.pid))

  def stop(self) -> float:
    self.stop_event.set()
    self.thread.join(timeout=2)
    self.peak_kib = max(self.peak_kib, process_tree_rss_kib(self.pid))
    return self.peak_kib / 1024


class BrowserProcess:
  def __init__(self, binary: Path):
    self.binary = binary
    self.temporary: tempfile.TemporaryDirectory[str] | None = None
    self.profile = Path()
    self.process: subprocess.Popen[bytes] | None = None
    self.monitor: RssMonitor | None = None
    self.endpoint = ""
    self.startup_ms = 0.0

  def __enter__(self) -> "BrowserProcess":
    self.temporary = tempfile.TemporaryDirectory(
        prefix="hardened-perf-profile-", ignore_cleanup_errors=True)
    self.profile = Path(self.temporary.name)
    environment = os.environ.copy()
    if SANDBOX_HELPER.is_file():
      environment["CHROME_DEVEL_SANDBOX"] = str(SANDBOX_HELPER)
    command = [
        str(self.binary),
        f"--user-data-dir={self.profile}",
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "about:blank",
    ]
    started = time.perf_counter()
    self.process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
    )
    self.monitor = RssMonitor(self.process.pid)
    self.monitor.start()
    active_port = self.profile / "DevToolsActivePort"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
      if self.process.poll() is not None:
        raise RuntimeError(
            f"{self.binary} exited during startup with {self.process.returncode}")
      with contextlib.suppress(OSError, ValueError):
        lines = active_port.read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        self.endpoint = f"http://127.0.0.1:{port}"
        self.startup_ms = (time.perf_counter() - started) * 1000
        return self
      time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {self.binary} DevTools endpoint")

  def __exit__(self, *_args: Any) -> None:
    if self.process:
      # Chromium has several child processes that can keep writing into the
      # profile after the browser process exits. Because the browser starts a
      # dedicated session, terminate the complete process group before trying
      # to remove its profile.
      process_group = self.process.pid
      with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
      with contextlib.suppress(subprocess.TimeoutExpired):
        self.process.wait(timeout=5)
      if self.process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
          os.killpg(process_group, signal.SIGKILL)
        self.process.wait(timeout=5)
      else:
        # The browser parent may have exited before a renderer or utility
        # process. A final group kill is harmless if the group is already gone.
        with contextlib.suppress(ProcessLookupError):
          os.killpg(process_group, signal.SIGKILL)
    if self.monitor:
      self.monitor.stop()
    if self.temporary:
      self.temporary.cleanup()

  @property
  def peak_rss_mib(self) -> float:
    return self.monitor.peak_kib / 1024 if self.monitor else 0.0

  def open_tab(self, url: str) -> CdpWebSocket:
    target = cdp_create_tab(self.endpoint, url)
    websocket_url = str(target.get("webSocketDebuggerUrl") or "")
    if not websocket_url:
      raise RuntimeError("CDP did not return a websocket URL")
    cdp = CdpWebSocket(websocket_url)
    cdp.connect()
    cdp.command("Runtime.enable")
    cdp.command("Page.enable")
    return cdp


def wait_for_ready(
    cdp: CdpWebSocket,
    timeout: float = 30,
    expected_url_part: str = "",
) -> None:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    with contextlib.suppress(Exception):
      state = evaluate(cdp, "({ready: document.readyState, href: location.href})", timeout=3)
      if (isinstance(state, dict) and state.get("ready") == "complete" and
          (not expected_url_part or expected_url_part in str(state.get("href") or ""))):
        return
    time.sleep(0.05)
  raise TimeoutError("document did not reach complete")


def benchmark_startup(binary: Path, _origin: str) -> dict[str, float]:
  with BrowserProcess(binary) as browser:
    return {"value": browser.startup_ms, "peakRssMiB": browser.peak_rss_mib}


def benchmark_speedometer(binary: Path, origin: str) -> dict[str, float]:
  with BrowserProcess(binary) as browser:
    cdp = browser.open_tab(origin + SPEEDOMETER_PATH)
    try:
      deadline = time.monotonic() + 360
      while time.monotonic() < deadline:
        text = str(evaluate(
            cdp, "document.body ? document.body.innerText : ''", timeout=5) or "")
        match = SCORE_RE.search(text)
        if match:
          return {
              "value": float(match.group(1)),
              "peakRssMiB": browser.peak_rss_mib,
          }
        time.sleep(0.2)
      raise TimeoutError("Speedometer 2.1 did not finish")
    finally:
      cdp.close()


def benchmark_dynamic_scrape(binary: Path, origin: str) -> dict[str, float]:
  expected = 600
  seen: set[str] = set()
  poll_sizes: list[int] = []
  with BrowserProcess(binary) as browser:
    cdp = browser.open_tab(origin + FIXTURE_PATH)
    try:
      wait_for_ready(cdp, expected_url_part="performance_fixture.html")
      evaluate(cdp, COLLECT_ITEMS_JS, timeout=10)
      evaluate(cdp, "window.startFixture()", timeout=3)
      started = time.perf_counter()
      stable = 0
      previous_count = -1
      deadline = time.monotonic() + 90
      iteration = 0
      while time.monotonic() < deadline:
        iteration += 1
        evaluate(cdp, "window.scrollTo(0, document.documentElement.scrollHeight)", timeout=3)
        state = evaluate(cdp, COLLECT_ITEMS_JS, timeout=10)
        poll_items = state.get("items", []) if isinstance(state, dict) else []
        poll_sizes.append(len(poll_items))
        if isinstance(state, dict):
          for item in poll_items:
            key = str(item.get("key") or "") if isinstance(item, dict) else ""
            if key:
              seen.add(key)
        fixture_state = evaluate(
            cdp,
            "({done: Boolean(window.fixtureDone), generated: Number(window.fixtureGenerated || 0)})",
            timeout=3)
        fixture_done = bool(
            fixture_state.get("done")) if isinstance(fixture_state, dict) else False
        generated = int(
            fixture_state.get("generated", 0)) if isinstance(fixture_state, dict) else 0
        if os.environ.get("HARDENED_PERFORMANCE_VERBOSE") == "1" and (
            iteration <= 5 or iteration % 10 == 0):
          print(
              f"    poll={iteration} generated={generated} "
              f"returned={len(poll_items)} unique={len(seen)}")
        if not fixture_done:
          evaluate(cdp, "window.advanceFixture()", timeout=3)
        if fixture_done and len(seen) == previous_count:
          stable += 1
        else:
          stable = 0
        previous_count = len(seen)
        if fixture_done and stable >= 10:
          break
        time.sleep(0.01)
      elapsed = time.perf_counter() - started
      return {
          "value": len(seen) / max(elapsed, 0.001),
          "items": float(len(seen)),
          "expectedItems": float(expected),
          "completeness": len(seen) / expected,
          "elapsedSeconds": elapsed,
          "pollIterations": float(iteration),
          "medianItemsPerPoll": statistics.median(poll_sizes) if poll_sizes else 0.0,
          "maximumItemsPerPoll": float(max(poll_sizes, default=0)),
          "peakRssMiB": browser.peak_rss_mib,
      }
    finally:
      cdp.close()


def benchmark_exports(item_count: int = 5000) -> dict[str, float]:
  with tempfile.TemporaryDirectory(prefix="hardened-export-perf-") as directory:
    job = Job(
        id="export-benchmark",
        app_id="benchmark",
        url="https://example.test/fixture",
        output_dir=Path(directory) / "job",
        config={"max_items": item_count, "checkpoint_items": 100},
    )
    raw_items = [{
        "key": f"item-{index}",
        "text": f"Benchmark item {index} " + ("searchable content " * 8),
        "author": f"author-{index % 31}",
        "permalink": f"https://example.test/items/{index}",
        "links": [f"https://example.test/items/{index}"],
    } for index in range(item_count)]
    started = time.perf_counter()
    added = add_items(job, raw_items, set())
    write_outputs(job, force=True)
    elapsed = time.perf_counter() - started
    bytes_written = sum(
        path.stat().st_size for path in job.output_dir.iterdir() if path.is_file())
    return {
        "items": float(added),
        "elapsedSeconds": elapsed,
        "itemsPerSecond": added / max(elapsed, 0.001),
        "bytesWritten": float(bytes_written),
    }


def median_samples(samples: list[dict[str, float]]) -> dict[str, float]:
  keys = sorted({key for sample in samples for key in sample})
  return {
      key: statistics.median(
          sample[key] for sample in samples if key in sample)
      for key in keys
  }


def ratio(candidate: dict[str, float], baseline: dict[str, float], key: str,
          higher_is_better: bool) -> float:
  left = candidate.get(key, 0)
  right = baseline.get(key, 0)
  if left <= 0 or right <= 0:
    return 1.0
  return left / right if higher_is_better else right / left


def candidate_score(candidate: dict[str, Any], baseline: dict[str, Any]) -> tuple[float, list[float]]:
  ratios = [
      ratio(candidate["startup"], baseline["startup"], "value", False),
      ratio(candidate["speedometer"], baseline["speedometer"], "value", True),
      ratio(candidate["dynamicScrape"], baseline["dynamicScrape"], "value", True),
  ]
  return math.prod(ratios) ** (1 / len(ratios)), ratios


def passes_gates(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
  score, ratios = candidate_score(candidate, baseline)
  del score
  completeness = candidate["dynamicScrape"].get("completeness", 0)
  candidate_rss = max(
      candidate[name].get("peakRssMiB", 0)
      for name in ("startup", "speedometer", "dynamicScrape"))
  baseline_rss = max(
      baseline[name].get("peakRssMiB", 0)
      for name in ("startup", "speedometer", "dynamicScrape"))
  return (
      all(value >= 0.95 for value in ratios) and
      completeness >= 0.995 and
      (baseline_rss <= 0 or
       candidate_rss <= baseline_rss * 1.15 + 1e-9))


def choose_variant(results: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
  decision: dict[str, Any] = {"comparisons": {}}
  if "portable" not in results:
    selected = "legacy" if "legacy" in results else next(iter(results))
    decision["reason"] = "portable benchmark unavailable"
    return selected, decision

  selected = "portable"
  if "legacy" in results:
    score, ratios = candidate_score(results["portable"], results["legacy"])
    passed = passes_gates(results["portable"], results["legacy"])
    decision["comparisons"]["portableVsLegacy"] = {
        "balancedRatio": score, "metricRatios": ratios, "passed": passed}
    if not passed or score < 1.0:
      selected = "legacy"

  if selected == "portable" and "zen4" in results:
    score, ratios = candidate_score(results["zen4"], results["portable"])
    passed = passes_gates(results["zen4"], results["portable"])
    decision["comparisons"]["zen4VsPortable"] = {
        "balancedRatio": score, "metricRatios": ratios, "passed": passed}
    if passed and score >= 1.02:
      selected = "zen4"

  decision["reason"] = "fastest candidate satisfying regression gates"
  return selected, decision


def cpu_supports_znver4() -> bool:
  try:
    cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
  except OSError:
    return False
  vendor = re.search(r"^vendor_id\s*:\s*(\S+)", cpuinfo, re.MULTILINE)
  flags = re.search(r"^flags\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
  return bool(
      vendor and vendor.group(1) == "AuthenticAMD" and flags and
      ZNVER4_FLAGS.issubset(set(flags.group(1).split())))


def perf_counter_access() -> dict[str, Any]:
  perf = shutil.which("perf")
  paranoid = None
  with contextlib.suppress(OSError, ValueError):
    paranoid = int(Path("/proc/sys/kernel/perf_event_paranoid").read_text())
  return {
      "perf": perf or "",
      "perfEventParanoid": paranoid,
      "hardwareCountersAvailable": bool(perf and paranoid is not None and paranoid <= 2),
  }


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(f".{path.name}.tmp")
  temporary.write_text(
      json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  temporary.replace(path)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--variant", action="append", choices=tuple(VARIANT_BINARIES),
                      help="Variant to benchmark; repeat to select multiple.")
  parser.add_argument(
      "--benchmark", action="append",
      choices=("startup", "speedometer", "dynamicScrape"),
      help="Benchmark to run; repeat to select multiple. Partial runs do not promote a build.")
  parser.add_argument("--runs", type=int, default=7)
  parser.add_argument("--warmups", type=int, default=3)
  parser.add_argument("--quick", action="store_true",
                      help="Use one warmup and three measured runs.")
  parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
  args = parser.parse_args()
  if args.quick:
    args.warmups = 1
    args.runs = 3
  if args.runs < 1 or args.warmups < 0:
    parser.error("runs must be positive and warmups cannot be negative")

  requested = args.variant or list(VARIANT_BINARIES)
  variants = [name for name in requested if VARIANT_BINARIES[name].is_file()]
  if "zen4" in variants and not cpu_supports_znver4():
    print("Skipping Zen 4: CPU compatibility check failed.")
    variants.remove("zen4")
  if not variants:
    raise SystemExit("No requested Hardened Chromium binaries exist.")

  samples: dict[str, dict[str, list[dict[str, float]]]] = {
      variant: {"startup": [], "speedometer": [], "dynamicScrape": []}
      for variant in variants
  }
  all_benchmarks: list[tuple[str, Callable[[Path, str], dict[str, float]]]] = [
      ("startup", benchmark_startup),
      ("speedometer", benchmark_speedometer),
      ("dynamicScrape", benchmark_dynamic_scrape),
  ]
  requested_benchmarks = set(args.benchmark or ())
  benchmarks = [
      entry for entry in all_benchmarks
      if not requested_benchmarks or entry[0] in requested_benchmarks
  ]

  with SourceServer() as server:
    for benchmark_name, benchmark in benchmarks:
      print(f"Benchmarking {benchmark_name}...")
      for repetition in range(args.warmups + args.runs):
        order = variants[repetition % len(variants):] + variants[:repetition % len(variants)]
        for variant in order:
          measured = repetition >= args.warmups
          label = "measure" if measured else "warmup"
          print(f"  {variant} {label} {repetition + 1}/{args.warmups + args.runs}")
          sample = benchmark(VARIANT_BINARIES[variant], server.origin)
          if measured:
            samples[variant][benchmark_name].append(sample)

  results = {
      variant: {
          benchmark_name: median_samples(values)
          for benchmark_name, values in benchmark_samples.items()
      }
      for variant, benchmark_samples in samples.items()
  }
  selection_ready = len(benchmarks) == len(all_benchmarks)
  if selection_ready:
    selected, decision = choose_variant(results)
  else:
    selected = "legacy" if "legacy" in results else "portable"
    decision = {
        "comparisons": {},
        "reason": "partial benchmark run cannot promote a build",
    }
  report = {
      "schemaVersion": 1,
      "product": "automation",
      "generatedAtEpoch": time.time(),
      "selectedVariant": selected,
      "selectionReady": selection_ready,
      "variants": {
          name: {
              "binary": str(VARIANT_BINARIES[name]),
              "metrics": results[name],
              "samples": samples[name],
          } for name in variants
      },
      "decision": decision,
      "exportPipeline": benchmark_exports(),
      "environment": {
          "cpuSupportsZnver4": cpu_supports_znver4(),
          **perf_counter_access(),
      },
      "policy": {
          "warmups": args.warmups,
          "runs": args.runs,
          "maximumMetricRegression": 0.03,
          "maximumMemoryRegression": 0.10,
          "zen4MinimumBalancedGain": 0.02,
      },
  }
  atomic_write_json(args.report.resolve(), report)
  print(f"Selected variant: {selected}")
  print(f"Report: {args.report.resolve()}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
