#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Manual multiprocess smoke test for the shared Hardened Chromium backend.

Each worker represents an independent local application. Workers concurrently
discover or start the service, submit one website to the broker, and verify
that the page produced a title, visible text, or extracted items. The parent
then checks that every worker observed the same Chromium and broker PIDs.

Tabs stay open by default so the shared blue browser window can be inspected.
Pass --close-tabs to close successful job tabs before the script exits.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import queue
import sys
import time
from typing import Any
import uuid

from hardened_scrape_client import AutoBrokerClient
from hardened_scrape_client import BrokerClientError


DEFAULT_URLS = (
    "https://example.com/",
    "https://www.iana.org/help/example-domains",
    "https://www.python.org/",
)


def wait_for_job_stream(
    client: AutoBrokerClient,
    job_id: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
  """Wait on the app-wide WebSocket and return final REST state plus metrics."""
  deadline = time.monotonic() + timeout_seconds
  event_count = 0
  last_sequence = 0
  terminal_status = ""
  resync_required = False
  websocket = client.open_event_stream({job_id: 0})
  try:
    while time.monotonic() < deadline:
      message = websocket.receive_json(
          min(1.0, max(0.01, deadline - time.monotonic())))
      if not message:
        continue
      if message.get("jobId") != job_id:
        continue
      if message.get("type") == "resync_required":
        resync_required = True
        break
      if "sequence" in message:
        event_count += 1
        last_sequence = max(last_sequence, int(message["sequence"]))
      if message.get("type") == "status":
        status = str(message.get("data", {}).get("status") or "")
        if status in {"completed", "failed", "stopped", "interrupted"}:
          terminal_status = status
          break
  finally:
    websocket.close()
  result = client.get_job(job_id)
  return result, {
      "transport": "websocket",
      "eventCount": event_count,
      "lastSequence": last_sequence,
      "terminalStatus": terminal_status,
      "resyncRequired": resync_required,
  }


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description=(
          "Run independent Python app processes against one visible Hardened "
          "Chromium backend."))
  parser.add_argument(
      "urls", nargs="*", default=list(DEFAULT_URLS),
      help="One URL per simulated app process.")
  parser.add_argument(
      "--service-timeout", type=int, default=120,
      help="Seconds each process allows for shared-service startup.")
  parser.add_argument(
      "--job-timeout", type=int, default=20,
      help="Broker-side collection timeout for each website.")
  parser.add_argument(
      "--wait-timeout", type=int, default=120,
      help="Maximum seconds each process waits for its job.")
  parser.add_argument(
      "--max-items", type=int, default=30,
      help="Maximum extracted items per website.")
  parser.add_argument(
      "--close-tabs", action="store_true",
      help="Close successfully parsed tabs instead of leaving them visible.")
  parser.add_argument(
      "--json", action="store_true",
      help="Print the final report as JSON.")
  return parser.parse_args()


def worker(
    index: int,
    url: str,
    run_id: str,
    start_event: Any,
    result_queue: Any,
    service_timeout: int,
    job_timeout: int,
    wait_timeout: int,
    max_items: int,
    close_tabs: bool,
) -> None:
  result: dict[str, Any] = {
      "worker": index,
      "processPid": multiprocessing.current_process().pid,
      "appId": f"manual_smoke_{run_id}_{index}",
      "url": url,
      "ok": False,
  }
  try:
    if not start_event.wait(timeout=15):
      raise RuntimeError("parent did not release the concurrent start barrier")

    client = AutoBrokerClient(
        app_id=result["appId"],
        timeout_seconds=service_timeout,
    )
    service = client.service_status
    result["browserPid"] = service.get("pids", {}).get("browser")
    result["brokerPid"] = service.get("pids", {}).get("broker")
    result["started"] = service.get("started", {})
    result["brokerUrl"] = service.get("broker", {}).get("url")

    submitted = client.submit_job(
        url,
        schema={
            "id": f"manual_page_{index}",
            "name": "Manual whole-page parser",
            "itemRoot": "body",
            "fields": [
                {"name": "heading", "selector": "h1", "mode": "text"},
                {"name": "text", "selector": "", "mode": "text"},
            ],
        },
        max_items=max_items,
        timeout_seconds=job_timeout,
        raw_snapshots=False,
    )
    job_id = str(submitted.get("job", {}).get("id") or "")
    if not job_id:
      raise RuntimeError("broker accepted the request without returning a job id")
    result["jobId"] = job_id

    completed, stream_metrics = wait_for_job_stream(
        client, job_id, wait_timeout)
    result["eventStream"] = stream_metrics
    job = completed.get("job", {})
    status = str(job.get("status") or "")
    result.update({
        "status": status,
        "reason": str(job.get("reason") or ""),
        "error": str(job.get("error") or ""),
        "title": str(job.get("title") or ""),
        "targetId": str(job.get("targetId") or ""),
        "itemCount": int(job.get("itemCount") or 0),
        "outputDir": str(job.get("outputDir") or ""),
    })
    if status != "completed":
      raise RuntimeError(
          f"job finished with status={status!r}: "
          f"{result['error'] or result['reason']}")

    visible_text = ""
    try:
      visible_text = client.download_file(job_id, "visible_text.txt").decode(
          "utf-8", "replace").strip()
    except BrokerClientError as error:
      # Simple pages can complete from title/items even if the optional visible
      # text snapshot was not emitted. Record that fact without discarding the
      # otherwise valid parse result.
      result["visibleTextError"] = str(error)
    items_response = client.get_items(job_id, limit=max_items)
    items = items_response.get("items", [])
    if not isinstance(items, list):
      items = []
    result["visibleTextLength"] = len(visible_text)
    result["visibleTextPreview"] = " ".join(visible_text.split())[:160]
    result["returnedItems"] = len(items)
    if items:
      item = items[0] if isinstance(items[0], dict) else {}
      fields = item.get("fields", {}) if isinstance(item, dict) else {}
      preview = (
          fields.get("heading") if isinstance(fields, dict) else "") or (
              item.get("text", "") if isinstance(item, dict) else "")
      result["itemPreview"] = " ".join(str(preview).split())[:160]
    result["parsed"] = bool(
        result["title"] or visible_text or result["itemCount"] or items)
    if not result["parsed"]:
      raise RuntimeError("page completed but produced no parseable output")
    if not result["targetId"]:
      raise RuntimeError("job did not create a visible Chromium tab target")

    if close_tabs:
      client.close_tab(job_id)
      result["tabClosed"] = True
    else:
      result["tabClosed"] = False
    result["ok"] = True
  except (BrokerClientError, OSError, RuntimeError, ValueError) as error:
    result["failure"] = f"{type(error).__name__}: {error}"
  except Exception as error:  # Keep worker failures visible to the parent.
    result["failure"] = f"unexpected {type(error).__name__}: {error}"
  finally:
    result_queue.put(result)


def validate_results(results: list[dict[str, Any]]) -> list[str]:
  failures = [
      f"worker {result.get('worker')}: {result.get('failure', 'failed')}"
      for result in results if not result.get("ok")
  ]
  successful = [result for result in results if result.get("ok")]
  if not successful:
    failures.append("no worker completed successfully")
    return failures

  browser_pids = {result.get("browserPid") for result in successful}
  broker_pids = {result.get("brokerPid") for result in successful}
  target_ids = {result.get("targetId") for result in successful}
  if None in browser_pids or len(browser_pids) != 1:
    failures.append(f"workers observed different browser PIDs: {browser_pids}")
  if None in broker_pids or len(broker_pids) != 1:
    failures.append(f"workers observed different broker PIDs: {broker_pids}")
  if len(target_ids) != len(successful):
    failures.append("workers did not receive distinct Chromium tab targets")

  browser_starts = sum(
      bool(result.get("started", {}).get("browser")) for result in successful)
  broker_starts = sum(
      bool(result.get("started", {}).get("broker")) for result in successful)
  if browser_starts > 1:
    failures.append(f"{browser_starts} workers claimed to start Chromium")
  if broker_starts > 1:
    failures.append(f"{broker_starts} workers claimed to start the broker")
  return failures


def print_human_report(
    results: list[dict[str, Any]], failures: list[str], elapsed: float) -> None:
  print("\nShared Hardened Chromium multiprocess smoke test")
  print("=" * 52)
  for result in results:
    marker = "PASS" if result.get("ok") else "FAIL"
    print(
        f"[{marker}] app={result.get('appId')} pid={result.get('processPid')} "
        f"browser={result.get('browserPid')} broker={result.get('brokerPid')}")
    print(f"       URL: {result.get('url')}")
    if result.get("ok"):
      print(
          f"       tab={result.get('targetId')} title={result.get('title')!r} "
          f"items={result.get('itemCount')} "
          f"visibleText={result.get('visibleTextLength')} bytes")
      if result.get("visibleTextPreview"):
        print(f"       text: {result['visibleTextPreview']}")
      elif result.get("itemPreview"):
        print(f"       parsed: {result['itemPreview']}")
    else:
      print(f"       error: {result.get('failure')}")

  print("-" * 52)
  if failures:
    print("FAILED")
    for failure in failures:
      print(f"  - {failure}")
  else:
    browser_pid = results[0].get("browserPid") if results else None
    broker_pid = results[0].get("brokerPid") if results else None
    print(
        f"PASSED: {len(results)} independent Python processes used browser "
        f"PID {browser_pid} and broker PID {broker_pid} with distinct tabs.")
  print(f"Elapsed: {elapsed:.1f}s")


def main() -> int:
  args = parse_args()
  if not args.urls:
    print("At least one URL is required.", file=sys.stderr)
    return 2
  if any(value <= 0 for value in (
      args.service_timeout, args.job_timeout, args.wait_timeout,
      args.max_items)):
    print("Timeouts and --max-items must be positive.", file=sys.stderr)
    return 2

  context = multiprocessing.get_context("spawn")
  start_event = context.Event()
  result_queue = context.Queue()
  run_id = uuid.uuid4().hex[:8]
  processes = []
  started_at = time.monotonic()
  for index, url in enumerate(args.urls, start=1):
    process = context.Process(
        target=worker,
        args=(
            index, url, run_id, start_event, result_queue,
            args.service_timeout, args.job_timeout, args.wait_timeout,
            args.max_items, args.close_tabs,
        ),
        name=f"manual-hardened-app-{index}",
    )
    process.start()
    processes.append(process)

  start_event.set()
  results: list[dict[str, Any]] = []
  collection_deadline = time.monotonic() + (
      args.service_timeout + args.wait_timeout + 30)
  while len(results) < len(processes):
    remaining = collection_deadline - time.monotonic()
    if remaining <= 0:
      break
    try:
      results.append(result_queue.get(timeout=min(remaining, 5)))
    except queue.Empty:
      if not any(process.is_alive() for process in processes):
        break

  for process in processes:
    process.join(timeout=5)
    if process.is_alive():
      process.terminate()
      process.join(timeout=5)

  received_workers = {result.get("worker") for result in results}
  for index, process in enumerate(processes, start=1):
    if index not in received_workers:
      results.append({
          "worker": index,
          "appId": f"manual_smoke_{run_id}_{index}",
          "url": args.urls[index - 1],
          "ok": False,
          "failure": f"worker exited with code {process.exitcode} without a result",
      })
  results.sort(key=lambda result: int(result.get("worker") or 0))

  failures = validate_results(results)
  elapsed = time.monotonic() - started_at
  report = {
      "ok": not failures,
      "elapsedSeconds": round(elapsed, 3),
      "workers": results,
      "failures": failures,
  }
  if args.json:
    print(json.dumps(report, indent=2, ensure_ascii=False))
  else:
    print_human_report(results, failures, elapsed)
  return 0 if not failures else 1


if __name__ == "__main__":
  raise SystemExit(main())
