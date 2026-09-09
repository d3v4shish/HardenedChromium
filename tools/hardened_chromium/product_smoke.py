#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Smoke-test Privacy and Automation binaries with disposable profiles."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen


DEFAULT_SANDBOX_HELPER = Path("/usr/local/sbin/chrome-devel-sandbox")


def reserve_port() -> int:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", 0))
    return int(listener.getsockname()[1])


def environment(sandbox_helper: Path) -> dict[str, str]:
  result = os.environ.copy()
  result["CHROME_DEVEL_SANDBOX"] = str(sandbox_helper)
  return result


def stop(process: subprocess.Popen[bytes]) -> None:
  if process.poll() is not None:
    return
  with contextlib.suppress(ProcessLookupError):
    os.killpg(process.pid, signal.SIGTERM)
  try:
    process.wait(timeout=5)
  except subprocess.TimeoutExpired:
    with contextlib.suppress(ProcessLookupError):
      os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def base_command(binary: Path, profile: Path, port: int) -> list[str]:
  return [
      str(binary),
      f"--user-data-dir={profile}",
      "--headless=new",
      "--remote-debugging-address=127.0.0.1",
      f"--remote-debugging-port={port}",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--disable-component-update",
  ]


def privacy_smoke(binary: Path, sandbox_helper: Path) -> dict[str, object]:
  port = reserve_port()
  with tempfile.TemporaryDirectory(prefix="hardened-privacy-smoke-") as name:
    profile = Path(name)
    process = subprocess.Popen(
        base_command(binary, profile, port) + ["about:blank"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=environment(sandbox_helper),
        start_new_session=True,
    )
    listener_seen = False
    deadline = time.monotonic() + 45
    try:
      ready_at: float | None = None
      while time.monotonic() < deadline:
        if process.poll() is not None:
          stderr = process.stderr.read() if process.stderr else b""
          raise RuntimeError(
              f"Privacy browser exited {process.returncode}: "
              f"{stderr.decode(errors='replace')[-2000:]}")
        with contextlib.suppress(OSError):
          with socket.create_connection(("127.0.0.1", port), timeout=0.05):
            listener_seen = True
        if (profile / "Local State").is_file():
          if ready_at is None:
            ready_at = time.monotonic()
          elif time.monotonic() - ready_at >= 2:
            break
        time.sleep(0.01)
      else:
        raise TimeoutError("Privacy profile did not initialize")
      marker = profile / "DevToolsActivePort"
      if marker.exists():
        raise RuntimeError("Privacy browser wrote DevToolsActivePort")
      if listener_seen:
        raise RuntimeError("Privacy browser exposed a remote-debugging listener")
      return {
          "port": port,
          "marker": False,
          "listener": False,
          "profileInitialized": True,
      }
    finally:
      stop(process)


def automation_smoke(binary: Path,
                     sandbox_helper: Path) -> dict[str, object]:
  port = reserve_port()
  with tempfile.TemporaryDirectory(prefix="hardened-automation-smoke-") as name:
    profile = Path(name)
    process = subprocess.Popen(
        base_command(binary, profile, port) + ["about:blank"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=environment(sandbox_helper),
        start_new_session=True,
    )
    marker = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 45
    try:
      lines: list[str] = []
      while time.monotonic() < deadline:
        if process.poll() is not None:
          stderr = process.stderr.read() if process.stderr else b""
          raise RuntimeError(
              f"Automation browser exited {process.returncode}: "
              f"{stderr.decode(errors='replace')[-2000:]}")
        with contextlib.suppress(OSError, ValueError):
          lines = marker.read_text(encoding="utf-8").splitlines()
          if len(lines) >= 2 and int(lines[0]) == port:
            break
        time.sleep(0.02)
      else:
        raise TimeoutError("Automation DevToolsActivePort marker was unavailable")
      if len(lines) < 2 or not lines[1].startswith("/devtools/browser/"):
        raise RuntimeError(f"Automation marker is invalid: {lines!r}")
      with urlopen(
          f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
        version = json.load(response)
      websocket_url = str(version.get("webSocketDebuggerUrl", ""))
      if not websocket_url.endswith(lines[1]):
        raise RuntimeError(
            "Automation endpoint GUID does not match profile marker")
      return {
          "port": port,
          "marker": True,
          "endpoint": True,
          "guidVerified": True,
      }
    finally:
      stop(process)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--privacy-binary", type=Path, required=True)
  parser.add_argument("--automation-binary", type=Path, required=True)
  parser.add_argument(
      "--sandbox-helper", type=Path, default=DEFAULT_SANDBOX_HELPER)
  args = parser.parse_args()
  if not args.sandbox_helper.is_file():
    parser.error(f"sandbox helper is missing: {args.sandbox_helper}")
  results = {
      "privacy": privacy_smoke(
          args.privacy_binary.resolve(), args.sandbox_helper.resolve()),
      "automation": automation_smoke(
          args.automation_binary.resolve(), args.sandbox_helper.resolve()),
  }
  print(json.dumps(results, sort_keys=True))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
