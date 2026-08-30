#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Benchmark the real app WebSocket path without launching Chromium."""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

from hardened_scrape_broker import (
    Broker,
    BrokerConfig,
    BrokerRequestHandler,
    Job,
)
from hardened_scrape_client import BrokerClient, BrokerEventWebSocket


DEFAULT_SOCKETS = 32
DEFAULT_EVENTS_PER_SECOND = 1000
DEFAULT_DURATION_SECONDS = 3.0
P95_GATE_MS = 25.0
P99_GATE_MS = 75.0


def percentile(values: list[float], percent: float) -> float:
  if not values:
    return float("inf")
  ordered = sorted(values)
  position = (len(ordered) - 1) * percent / 100.0
  lower = int(position)
  upper = min(lower + 1, len(ordered) - 1)
  fraction = position - lower
  return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def receive_until_subscribed(websocket: BrokerEventWebSocket) -> None:
  deadline = time.monotonic() + 5
  ready = False
  subscribed = False
  while time.monotonic() < deadline and not (ready and subscribed):
    message = websocket.receive_json(max(0.01, deadline - time.monotonic()))
    if not message:
      continue
    ready = ready or message.get("type") == "ready"
    subscribed = subscribed or message.get("type") == "subscribed"
  if not ready or not subscribed:
    raise RuntimeError("stream did not become ready")


def run_benchmark(
    socket_count: int = DEFAULT_SOCKETS,
    events_per_second: int = DEFAULT_EVENTS_PER_SECOND,
    duration_seconds: float = DEFAULT_DURATION_SECONDS,
) -> dict[str, Any]:
  expected_events = max(1, round(events_per_second * duration_seconds))
  with tempfile.TemporaryDirectory(prefix="hardened-stream-benchmark-") as directory:
    broker = Broker(BrokerConfig(
        cdp_endpoint="http://127.0.0.1:1",
        output_root=Path(directory),
        token="benchmark-admin",
        start_scheduler=False,
    ))
    server = ThreadingHTTPServer(("127.0.0.1", 0), BrokerRequestHandler)
    server.broker = broker
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    sockets: list[BrokerEventWebSocket] = []
    jobs: list[Job] = []
    collectors: list[threading.Thread] = []
    latencies_ms: list[float] = []
    latency_lock = threading.Lock()
    stop_collectors = threading.Event()
    collector_errors: list[str] = []
    connect_ms: list[float] = []

    try:
      for index in range(socket_count):
        app = broker.register_app({
            "name": f"Benchmark app {index}",
            "app_id": f"benchmark-app-{index}",
        })
        job = Job(
            id=f"benchmark-job-{index}",
            app_id=app.id,
            url="https://benchmark.invalid/",
            output_dir=Path(directory) / app.id / f"job-{index}",
            config={},
            status="running",
        )
        with broker.jobs_lock:
          broker.jobs[job.id] = job
        jobs.append(job)
        client = BrokerClient(
            origin, app_id=app.id, app_secret=app.secret)
        started = time.perf_counter()
        websocket = client.open_event_stream({job.id: 0})
        receive_until_subscribed(websocket)
        connect_ms.append((time.perf_counter() - started) * 1000)
        sockets.append(websocket)

      def collect(websocket: BrokerEventWebSocket) -> None:
        try:
          while not stop_collectors.is_set():
            message = websocket.receive_json(0.1)
            if not message or message.get("type") != "benchmark":
              continue
            published_ns = int(message.get("data", {}).get("publishedNs") or 0)
            if published_ns:
              latency = (time.perf_counter_ns() - published_ns) / 1_000_000
              with latency_lock:
                latencies_ms.append(latency)
        except Exception as error:  # Preserve collector failures in the report.
          if not stop_collectors.is_set():
            collector_errors.append(f"{type(error).__name__}: {error}")

      for websocket in sockets:
        thread = threading.Thread(target=collect, args=(websocket,), daemon=True)
        thread.start()
        collectors.append(thread)

      interval = 1.0 / events_per_second
      benchmark_started = time.perf_counter()
      next_publish = benchmark_started
      for index in range(expected_events):
        next_publish = benchmark_started + index * interval
        remaining = next_publish - time.perf_counter()
        if remaining > 0:
          time.sleep(remaining)
        broker.add_event(jobs[index % socket_count], "benchmark", {
            "publishedNs": time.perf_counter_ns(),
            "index": index,
        })
      publish_elapsed = time.perf_counter() - benchmark_started

      deadline = time.monotonic() + max(5.0, duration_seconds)
      while len(latencies_ms) < expected_events and time.monotonic() < deadline:
        time.sleep(0.005)
      delivery_elapsed = time.perf_counter() - benchmark_started
      stop_collectors.set()
      for websocket in sockets:
        websocket.close()
      for thread in collectors:
        thread.join(timeout=1)

      delivered = len(latencies_ms)
      p95 = percentile(latencies_ms, 95)
      p99 = percentile(latencies_ms, 99)
      return {
          "ok": (
              delivered == expected_events and not collector_errors and
              p95 <= P95_GATE_MS and p99 <= P99_GATE_MS),
          "configuration": {
              "sockets": socket_count,
              "eventsPerSecond": events_per_second,
              "durationSeconds": duration_seconds,
              "expectedEvents": expected_events,
          },
          "delivery": {
              "deliveredEvents": delivered,
              "lostEvents": expected_events - delivered,
              "publishElapsedSeconds": publish_elapsed,
              "endToEndElapsedSeconds": delivery_elapsed,
              "achievedPublishRate": expected_events / publish_elapsed,
          },
          "latencyMs": {
              "p50": percentile(latencies_ms, 50),
              "p95": p95,
              "p99": p99,
              "maximum": max(latencies_ms, default=float("inf")),
          },
          "connectMs": {
              "p50": percentile(connect_ms, 50),
              "p95": percentile(connect_ms, 95),
              "maximum": max(connect_ms, default=float("inf")),
          },
          "gates": {"p95Ms": P95_GATE_MS, "p99Ms": P99_GATE_MS},
          "collectorErrors": collector_errors,
      }
    finally:
      stop_collectors.set()
      for websocket in sockets:
        websocket.close()
      server.shutdown()
      server.server_close()
      server_thread.join(timeout=2)
      broker.close()


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--sockets", type=int, default=DEFAULT_SOCKETS)
  parser.add_argument(
      "--events-per-second", type=int, default=DEFAULT_EVENTS_PER_SECOND)
  parser.add_argument("--duration-seconds", type=float,
                      default=DEFAULT_DURATION_SECONDS)
  parser.add_argument(
      "--require-gates", action="store_true",
      help="Return a nonzero status when delivery or latency gates fail.")
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  if args.sockets < 1 or args.events_per_second < 1 or args.duration_seconds <= 0:
    raise SystemExit("benchmark arguments must be positive")
  report = run_benchmark(
      args.sockets, args.events_per_second, args.duration_seconds)
  print(json.dumps(report, indent=2))
  return 1 if args.require_gates and not report["ok"] else 0


if __name__ == "__main__":
  raise SystemExit(main())
