#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Dependency-free client for the local Hardened Scrape Broker."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode, urlparse
import urllib.error
import urllib.request


DEFAULT_BROKER_URL = "http://127.0.0.1:8877"
DEFAULT_SERVICE_SCRIPT = Path(__file__).resolve().with_name(
    "hardened_scrape_service.py")
TERMINAL_STATUSES = {"completed", "failed", "stopped", "interrupted"}


class BrokerClientError(RuntimeError):
  pass


def resolve_service_script(service_script: Path | str | None = None) -> Path:
  """Find the installed service wrapper without probing browser processes."""
  if service_script is not None:
    return Path(service_script).expanduser()
  configured = os.environ.get("HARDENED_SCRAPE_SERVICE", "").strip()
  if configured:
    return Path(configured).expanduser()
  installed = shutil.which("hardened-chromium-service")
  if installed:
    return Path(installed)
  return DEFAULT_SERVICE_SCRIPT


class BrokerEventWebSocket:
  """Small RFC 6455 client for the broker's local app event stream."""

  def __init__(self, websocket_url: str):
    self.websocket_url = websocket_url
    self.sock: socket.socket | None = None
    self.receive_buffer = bytearray()
    self.send_lock = threading.Lock()

  def connect(self, timeout: float = 10) -> None:
    parsed = urlparse(self.websocket_url)
    if parsed.scheme != "ws" or not parsed.hostname:
      raise BrokerClientError("broker returned an invalid WebSocket URL")
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
      path += f"?{parsed.query}"
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    try:
      sock.sendall(request.encode("ascii"))
      response = bytearray()
      while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
          raise BrokerClientError("WebSocket handshake closed")
        response.extend(chunk)
        if len(response) > 65536:
          raise BrokerClientError("WebSocket handshake was too large")
      headers, remaining = bytes(response).split(b"\r\n\r\n", 1)
      lines = headers.decode("iso-8859-1").split("\r\n")
      if " 101 " not in lines[0]:
        raise BrokerClientError(f"WebSocket handshake failed: {lines[0]}")
      response_headers: dict[str, str] = {}
      for line in lines[1:]:
        if ":" in line:
          name, value = line.split(":", 1)
          response_headers[name.strip().lower()] = value.strip()
      expected = base64.b64encode(hashlib.sha1(  # nosec: RFC 6455.
          (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(
              "ascii")).digest()).decode("ascii")
      if response_headers.get("sec-websocket-accept") != expected:
        raise BrokerClientError("WebSocket handshake signature was invalid")
      self.receive_buffer.extend(remaining)
      self.sock = sock
      sock.settimeout(None)
    except Exception:
      sock.close()
      raise

  def send_json(self, value: dict[str, Any]) -> None:
    self._send_frame(
        json.dumps(value, separators=(",", ":")).encode("utf-8"), 0x1)

  def subscribe(self, subscriptions: dict[str, int]) -> None:
    self.send_json({
        "type": "subscribe",
        "subscriptions": [
            {"jobId": job_id, "afterSequence": max(0, int(sequence))}
            for job_id, sequence in subscriptions.items()
        ],
    })

  def unsubscribe(self, job_ids: list[str]) -> None:
    self.send_json({"type": "unsubscribe", "jobIds": job_ids})

  def receive_json(self, timeout: float | None = None) -> dict[str, Any] | None:
    if not self.sock:
      raise BrokerClientError("WebSocket is not connected")
    self.sock.settimeout(timeout)
    while True:
      try:
        fin, opcode, payload = self._receive_frame()
      except socket.timeout:
        return None
      if opcode == 0x8:
        code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
        reason = payload[2:].decode("utf-8", "replace")
        raise BrokerClientError(f"WebSocket closed ({code}): {reason}")
      if opcode == 0x9:
        self._send_frame(payload, 0xA)
        continue
      if opcode == 0xA:
        continue
      if not fin or opcode != 0x1:
        raise BrokerClientError("unsupported fragmented/binary WebSocket message")
      try:
        value = json.loads(payload.decode("utf-8"))
      except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerClientError("broker sent invalid WebSocket JSON") from error
      if not isinstance(value, dict):
        raise BrokerClientError("broker sent a non-object WebSocket message")
      return value

  def close(self) -> None:
    sock = self.sock
    if not sock:
      return
    try:
      with self.send_lock:
        self._send_frame_to(sock, struct.pack("!H", 1000), 0x8)
    except OSError:
      pass
    try:
      sock.shutdown(socket.SHUT_RDWR)
    except OSError:
      pass
    sock.close()
    if self.sock is sock:
      self.sock = None

  def __enter__(self) -> "BrokerEventWebSocket":
    if not self.sock:
      self.connect()
    return self

  def __exit__(self, *_args: Any) -> None:
    self.close()

  def _send_frame(self, payload: bytes, opcode: int) -> None:
    if not self.sock:
      raise BrokerClientError("WebSocket is not connected")
    with self.send_lock:
      self._send_frame_to(self.sock, payload, opcode)

  @staticmethod
  def _send_frame_to(sock: socket.socket, payload: bytes, opcode: int) -> None:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
      header.append(0x80 | length)
    elif length <= 0xFFFF:
      header.append(0x80 | 126)
      header.extend(struct.pack("!H", length))
    else:
      header.append(0x80 | 127)
      header.extend(struct.pack("!Q", length))
    mask = os.urandom(4)
    body = bytes(byte ^ mask[index % 4]
                 for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + mask + body)

  def _receive_frame(self) -> tuple[bool, int, bytes]:
    first = self._receive_exact(2)
    fin = bool(first[0] & 0x80)
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
      length = struct.unpack("!H", self._receive_exact(2))[0]
    elif length == 127:
      length = struct.unpack("!Q", self._receive_exact(8))[0]
    if length > 4 * 1024 * 1024:
      raise BrokerClientError("broker WebSocket frame exceeds 4 MiB")
    mask = self._receive_exact(4) if masked else b""
    payload = self._receive_exact(length)
    if masked:
      payload = bytes(byte ^ mask[index % 4]
                      for index, byte in enumerate(payload))
    return fin, opcode, payload

  def _receive_exact(self, length: int) -> bytes:
    if not self.sock:
      raise BrokerClientError("WebSocket is not connected")
    while len(self.receive_buffer) < length:
      chunk = self.sock.recv(max(4096, length - len(self.receive_buffer)))
      if not chunk:
        raise BrokerClientError("WebSocket closed unexpectedly")
      self.receive_buffer.extend(chunk)
    value = bytes(self.receive_buffer[:length])
    del self.receive_buffer[:length]
    return value


def ensure_service(
    *,
    service_script: Path | str | None = None,
    timeout_seconds: int = 90,
    broker_url: str = "",
    cdp_endpoint: str = "",
    state_dir: Path | str | None = None,
    output_root: Path | str | None = None,
    no_auth: bool = False,
) -> dict[str, Any]:
  """Start/reuse the local scrape service and return its JSON status."""
  resolved_service_script = resolve_service_script(service_script)
  command = [
      sys.executable,
      str(resolved_service_script),
      "--json",
      "--timeout-seconds",
      str(timeout_seconds),
  ]
  if no_auth:
    command.append("--no-auth")
  if broker_url:
    command.extend(["--broker", broker_url])
  if cdp_endpoint:
    command.extend(["--cdp", cdp_endpoint])
  if state_dir is not None:
    command.extend(["--state-dir", str(state_dir)])
  if output_root is not None:
    command.extend(["--output-root", str(output_root)])
  command.append("ensure")

  try:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(timeout_seconds + 10, 30),
        check=False,
    )
  except subprocess.TimeoutExpired as error:
    raise BrokerClientError(
        f"scrape service did not become ready within {timeout_seconds}s"
    ) from error
  except OSError as error:
    raise BrokerClientError(f"failed to run scrape service helper: {error}") from error

  output = completed.stdout.strip()
  if not output:
    detail = completed.stderr.strip() or f"exit code {completed.returncode}"
    raise BrokerClientError(f"scrape service returned no JSON: {detail}")
  try:
    payload = json.loads(output)
  except json.JSONDecodeError as error:
    raise BrokerClientError(
        f"scrape service returned invalid JSON: {output[:1000]}"
    ) from error
  if not isinstance(payload, dict):
    raise BrokerClientError("scrape service returned a non-object JSON response")
  if completed.returncode != 0 or payload.get("ok") is False:
    raise BrokerClientError(str(payload.get("error") or completed.stderr.strip()))
  return payload


class BrokerClient:
  def __init__(
      self,
      broker_url: str = DEFAULT_BROKER_URL,
      token: str = "",
      app_id: str = "",
      app_secret: str = "",
  ):
    self.broker_url = broker_url.rstrip("/")
    self.token = token
    self.app_id = app_id
    self.app_secret = app_secret

  def register_app(
      self,
      name: str,
      app_id: str = "",
      description: str = "",
  ) -> dict[str, Any]:
    return self.request_json("/apps", "POST", {
        "name": name,
        "app_id": app_id,
        "description": description,
    })

  def list_apps(self) -> dict[str, Any]:
    return self.request_json("/apps")

  def submit_job(
      self,
      url: str,
      *,
      app_id: str = "",
      schema_id: str = "",
      schema: dict[str, Any] | None = None,
      max_items: int | None = None,
      timeout_seconds: int | None = None,
      raw_snapshots: bool | None = None,
  ) -> dict[str, Any]:
    body: dict[str, Any] = {"url": url}
    if app_id:
      body["app_id"] = app_id
    if schema_id:
      body["schema_id"] = schema_id
    if schema is not None:
      body["schema"] = schema
    if max_items is not None:
      body["max_items"] = max_items
    if timeout_seconds is not None:
      body["timeout_seconds"] = timeout_seconds
    if raw_snapshots is not None:
      body["raw_snapshots"] = raw_snapshots
    return self.request_json("/jobs", "POST", body)

  def list_jobs(self) -> dict[str, Any]:
    return self.request_json("/jobs")

  def list_schemas(self) -> dict[str, Any]:
    return self.request_json("/schemas")

  def get_schema(self, schema_id: str) -> dict[str, Any]:
    return self.request_json(f"/schemas/{url_component(schema_id)}")

  def save_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
    return self.request_json("/schemas", "POST", schema)

  def delete_schema(self, schema_id: str) -> dict[str, Any]:
    return self.request_json(f"/schemas/{url_component(schema_id)}", "DELETE")

  def list_feeds(self) -> dict[str, Any]:
    return self.request_json("/feeds")

  def get_feed(self, feed_id: str) -> dict[str, Any]:
    return self.request_json(f"/feeds/{url_component(feed_id)}")

  def save_feed(self, feed: dict[str, Any]) -> dict[str, Any]:
    return self.request_json("/feeds", "POST", feed)

  def delete_feed(self, feed_id: str) -> dict[str, Any]:
    return self.request_json(f"/feeds/{url_component(feed_id)}", "DELETE")

  def run_feed(
      self,
      feed_id: str,
      *,
      app_id: str = "",
      url: str = "",
      max_items: int | None = None,
      timeout_seconds: int | None = None,
      raw_snapshots: bool | None = None,
  ) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if app_id:
      body["app_id"] = app_id
    if url:
      body["url"] = url
    if max_items is not None:
      body["max_items"] = max_items
    if timeout_seconds is not None:
      body["timeout_seconds"] = timeout_seconds
    if raw_snapshots is not None:
      body["raw_snapshots"] = raw_snapshots
    return self.request_json(f"/feeds/{url_component(feed_id)}/run", "POST", body)

  def download_latest_feed_file(self, feed_id: str, remote_name: str) -> bytes:
    return self.request_bytes(
        f"/feeds/{url_component(feed_id)}/latest/{url_component(remote_name)}")

  def get_job(self, job_id: str, include_items: bool = True) -> dict[str, Any]:
    del include_items
    return self.request_json(f"/jobs/{url_component(job_id)}")

  def get_items(self, job_id: str, since: int = 0, limit: int = 0) -> dict[str, Any]:
    query = urlencode({"since": since, "limit": limit})
    return self.request_json(f"/jobs/{url_component(job_id)}/items?{query}")

  def resume_job(self, job_id: str) -> dict[str, Any]:
    return self.request_json(f"/jobs/{url_component(job_id)}/resume", "POST")

  def stop_job(self, job_id: str) -> dict[str, Any]:
    return self.request_json(f"/jobs/{url_component(job_id)}/stop", "POST")

  def retry_job(self, job_id: str) -> dict[str, Any]:
    return self.request_json(f"/jobs/{url_component(job_id)}/retry", "POST")

  def close_tab(self, job_id: str) -> dict[str, Any]:
    return self.request_json(f"/jobs/{url_component(job_id)}/close-tab", "POST")

  def close_completed_tabs(self) -> dict[str, Any]:
    return self.request_json("/jobs/close-completed-tabs", "POST")

  def preview_schema(
      self,
      job_id: str,
      *,
      schema: dict[str, Any] | None = None,
      schema_id: str = "",
  ) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if schema is not None:
      body["schema"] = schema
    if schema_id:
      body["schema_id"] = schema_id
    return self.request_json(
        f"/jobs/{url_component(job_id)}/preview-schema",
        "POST",
        body)

  def control_job(self, job_id: str, action: str, **kwargs: Any) -> dict[str, Any]:
    body = {"action": action}
    body.update(kwargs)
    return self.request_json(
        f"/jobs/{url_component(job_id)}/control",
        "POST",
        body)

  def pick_selector(self, job_id: str, timeout_seconds: int = 120) -> dict[str, Any]:
    return self.control_job(
        job_id,
        "pick_selector",
        timeout_seconds=timeout_seconds)

  def page_summary(self, job_id: str) -> dict[str, Any]:
    return self.control_job(job_id, "page_summary")

  def click(self, job_id: str, selector: str) -> dict[str, Any]:
    return self.control_job(job_id, "click", selector=selector)

  def type_text(self, job_id: str, selector: str, text: str) -> dict[str, Any]:
    return self.control_job(job_id, "type", selector=selector, text=text)

  def scroll(self, job_id: str, x: int = 0, y: int = 800) -> dict[str, Any]:
    return self.control_job(job_id, "scroll", x=x, y=y)

  def screenshot(self, job_id: str, full_page: bool = False) -> dict[str, Any]:
    return self.control_job(job_id, "screenshot", full_page=full_page)

  def download_file(self, job_id: str, remote_name: str) -> bytes:
    return self.request_bytes(f"/jobs/{url_component(job_id)}/{url_component(remote_name)}")

  def event_log(self, job_id: str) -> str:
    return self.download_file(job_id, "events.jsonl").decode("utf-8")

  def create_stream_ticket(self) -> dict[str, Any]:
    """Create a single-use, short-lived ticket for a WebSocket upgrade."""
    return self.request_json("/stream-tickets", "POST", {})

  def open_event_stream(
      self,
      subscriptions: dict[str, int] | None = None,
  ) -> BrokerEventWebSocket:
    """Open one app-wide stream and optionally subscribe to many jobs."""
    ticket = self.create_stream_ticket()
    websocket = BrokerEventWebSocket(str(ticket["webSocketUrl"]))
    websocket.connect()
    if subscriptions:
      websocket.subscribe(subscriptions)
    return websocket

  def stream_app_events(self, subscriptions: dict[str, int]):
    """Yield multiplexed job events until the connection closes."""
    websocket = self.open_event_stream(subscriptions)
    try:
      while True:
        message = websocket.receive_json()
        if message is not None:
          yield message
    finally:
      websocket.close()

  def stream_events(
      self,
      job_id: str,
      since: str = "latest",
      after_sequence: int | None = None,
  ):
    query_values: dict[str, str | int] = {"since": since}
    if after_sequence is not None:
      query_values["afterSequence"] = max(0, after_sequence)
    query = urlencode(query_values)
    request = self.make_request(f"/jobs/{url_component(job_id)}/events?{query}")
    with urllib.request.urlopen(request, timeout=None) as response:
      event = "message"
      data_lines: list[str] = []
      for raw_line in response:
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("event:"):
          event = line[6:].strip()
        elif line.startswith("data:"):
          data_lines.append(line[5:].lstrip())
        elif not line:
          if data_lines:
            data = "\n".join(data_lines)
            try:
              payload: Any = json.loads(data)
            except json.JSONDecodeError:
              payload = data
            yield event, payload
          event = "message"
          data_lines = []

  def request_json(
      self,
      path: str,
      method: str = "GET",
      body: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    data = self.request_bytes(path, method, body)
    try:
      value = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as error:
      raise BrokerClientError(f"broker returned non-JSON response: {error}") from error
    if not isinstance(value, dict):
      raise BrokerClientError("broker returned a non-object JSON response")
    if value.get("ok") is False:
      raise BrokerClientError(str(value.get("error") or "broker request failed"))
    return value

  def request_bytes(
      self,
      path: str,
      method: str = "GET",
      body: dict[str, Any] | None = None,
  ) -> bytes:
    request = self.make_request(path, method, body)
    try:
      with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()
    except urllib.error.HTTPError as error:
      detail = error.read().decode("utf-8", "replace")
      try:
        payload = json.loads(detail)
        message = payload.get("error") or detail
      except json.JSONDecodeError:
        message = detail or error.reason
      raise BrokerClientError(f"HTTP {error.code}: {message}") from error
    except urllib.error.URLError as error:
      raise BrokerClientError(str(error)) from error

  def make_request(
      self,
      path: str,
      method: str = "GET",
      body: dict[str, Any] | None = None,
  ) -> urllib.request.Request:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
      data = json.dumps(body).encode("utf-8")
      headers["Content-Type"] = "application/json"
    if self.token:
      headers["Authorization"] = f"Bearer {self.token}"
    if self.app_id and self.app_secret:
      headers["X-Hardened-App-Id"] = self.app_id
      headers["X-Hardened-App-Secret"] = self.app_secret
    return urllib.request.Request(
        self.broker_url + path,
        data=data,
        headers=headers,
        method=method,
    )


class AutoBrokerClient(BrokerClient):
  """BrokerClient that starts/reuses the local service before first use."""

  def __init__(
      self,
      *,
      app_id: str = "",
      broker_url: str = "",
      token: str = "",
      app_secret: str = "",
      cdp_endpoint: str = "",
      service_script: Path | str | None = None,
      state_dir: Path | str | None = None,
      output_root: Path | str | None = None,
      timeout_seconds: int = 90,
      no_auth: bool = False,
  ):
    service = ensure_service(
        service_script=service_script,
        timeout_seconds=timeout_seconds,
        broker_url=broker_url,
        cdp_endpoint=cdp_endpoint,
        state_dir=state_dir,
        output_root=output_root,
        no_auth=no_auth,
    )
    resolved_broker_url = broker_url or str(
        service.get("broker", {}).get("url") or DEFAULT_BROKER_URL)
    resolved_token = token
    if not no_auth and not resolved_token and not app_secret:
      resolved_token = str(service.get("token") or "")

    # With app credentials, use app-scoped headers. Without app credentials,
    # use the service admin token and apply app_id as the default submitted job
    # owner. In no-auth mode, the broker accepts loopback requests without
    # headers and app_id remains a job-owner label.
    header_app_id = app_id if app_secret and not no_auth else ""
    super().__init__(
        broker_url=resolved_broker_url,
        token=resolved_token,
        app_id=header_app_id,
        app_secret=app_secret,
    )
    self.default_job_app_id = "" if app_secret and not no_auth else app_id
    self._service_script = service_script
    self._service_timeout_seconds = timeout_seconds
    self._service_cdp_endpoint = cdp_endpoint
    self._service_state_dir = state_dir
    self._service_output_root = output_root
    self.service_status = service
    self.no_auth = no_auth

  def ensure_backend(self) -> dict[str, Any]:
    """Recover the shared browser/broker before starting new browser work."""
    service = ensure_service(
        service_script=self._service_script,
        timeout_seconds=self._service_timeout_seconds,
        broker_url=self.broker_url,
        cdp_endpoint=self._service_cdp_endpoint,
        state_dir=self._service_state_dir,
        output_root=self._service_output_root,
        no_auth=self.no_auth,
    )
    self.service_status = service
    self.broker_url = str(
        service.get("broker", {}).get("url") or self.broker_url).rstrip("/")
    if not self.no_auth and not self.app_secret:
      self.token = str(service.get("token") or self.token)
    return service

  def submit_job(
      self,
      url: str,
      *,
      app_id: str = "",
      schema_id: str = "",
      schema: dict[str, Any] | None = None,
      max_items: int | None = None,
      timeout_seconds: int | None = None,
      raw_snapshots: bool | None = None,
  ) -> dict[str, Any]:
    self.ensure_backend()
    return super().submit_job(
        url,
        app_id=app_id or self.default_job_app_id,
        schema_id=schema_id,
        schema=schema,
        max_items=max_items,
        timeout_seconds=timeout_seconds,
        raw_snapshots=raw_snapshots,
    )

  def run_feed(
      self,
      feed_id: str,
      *,
      app_id: str = "",
      url: str = "",
      max_items: int | None = None,
      timeout_seconds: int | None = None,
      raw_snapshots: bool | None = None,
  ) -> dict[str, Any]:
    self.ensure_backend()
    return super().run_feed(
        feed_id,
        app_id=app_id or self.default_job_app_id,
        url=url,
        max_items=max_items,
        timeout_seconds=timeout_seconds,
        raw_snapshots=raw_snapshots,
    )


class FreeBrokerClient(AutoBrokerClient):
  """Auto-starting broker client for loopback-only no-auth mode."""

  def __init__(self, **kwargs: Any):
    kwargs["no_auth"] = True
    super().__init__(**kwargs)


def url_component(value: str) -> str:
  return quote(str(value), safe="")


def print_json(value: Any) -> None:
  print(json.dumps(value, indent=2, ensure_ascii=False))


def load_json_file(path: Path) -> dict[str, Any]:
  value = json.loads(path.read_text(encoding="utf-8"))
  if not isinstance(value, dict):
    raise BrokerClientError(f"{path} must contain a JSON object")
  return value


def wait_for_job(
    client: BrokerClient,
    job_id: str,
    poll_interval: float,
    wait_timeout: float,
) -> dict[str, Any]:
  deadline = time.monotonic() + wait_timeout if wait_timeout > 0 else None
  while True:
    result = client.get_job(job_id)
    job = result.get("job", {})
    status = str(job.get("status") or "")
    if status in TERMINAL_STATUSES or status == "needs_user":
      return result
    if deadline is not None and time.monotonic() >= deadline:
      return result
    time.sleep(max(0.25, poll_interval))


def download_outputs(client: BrokerClient, job_id: str, output_dir: Path) -> list[Path]:
  output_dir.mkdir(parents=True, exist_ok=True)
  names = [
      "latest.html",
      "latest.json",
      "items.jsonl",
      "items.csv",
      "feed.json",
      "feed.rss",
      "feed.atom",
      "visible_text.txt",
      "events.jsonl",
      "raw.html",
      "snapshot.mhtml",
      "manifest.json",
  ]
  written: list[Path] = []
  for name in names:
    try:
      data = client.download_file(job_id, name)
    except BrokerClientError:
      continue
    path = output_dir / name
    path.write_bytes(data)
    written.append(path)
  return written


def download_feed_outputs(
    client: BrokerClient,
    feed_id: str,
    output_dir: Path,
) -> list[Path]:
  output_dir.mkdir(parents=True, exist_ok=True)
  names = [
      "latest.html",
      "latest.json",
      "items.jsonl",
      "items.csv",
      "feed.json",
      "feed.rss",
      "feed.atom",
      "visible_text.txt",
  ]
  written: list[Path] = []
  for name in names:
    try:
      data = client.download_latest_feed_file(feed_id, name)
    except BrokerClientError:
      continue
    path = output_dir / name
    path.write_bytes(data)
    written.append(path)
  return written


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Client for Hardened Scrape Broker.")
  parser.add_argument("--broker", default=os.environ.get("HARDENED_BROKER_URL", DEFAULT_BROKER_URL))
  parser.add_argument("--token", default=os.environ.get("HARDENED_BROKER_TOKEN", ""))
  parser.add_argument("--app-id", default=os.environ.get("HARDENED_APP_ID", ""))
  parser.add_argument("--app-secret", default=os.environ.get("HARDENED_APP_SECRET", ""))

  subparsers = parser.add_subparsers(dest="command", required=True)

  ensure = subparsers.add_parser("ensure-service", help="Start/reuse local scrape service.")
  ensure.add_argument("--timeout-seconds", type=int, default=90)
  ensure.add_argument("--cdp", default="")
  ensure.add_argument("--state-dir", type=Path)
  ensure.add_argument("--output-root", type=Path)
  ensure.add_argument(
      "--no-auth",
      action="store_true",
      default=os.environ.get("HARDENED_BROKER_NO_AUTH", "")
      .strip().lower() in ("1", "true", "yes", "on"))

  register = subparsers.add_parser("register-app", help="Register a local app.")
  register.add_argument("--name", required=True)
  register.add_argument("--id", default="", dest="requested_app_id")
  register.add_argument("--description", default="")

  subparsers.add_parser("apps", help="List registered apps.")
  subparsers.add_parser("jobs", help="List visible jobs.")
  subparsers.add_parser("feeds", help="List user-facing saved feeds.")
  subparsers.add_parser("schemas", help="List saved extraction schemas.")

  feed_get = subparsers.add_parser("feed", help="Get one saved feed.")
  feed_get.add_argument("feed_id")

  feed_save = subparsers.add_parser("save-feed", help="Create or update a user-facing feed from JSON.")
  feed_save.add_argument("--file", type=Path, required=True)

  feed_delete = subparsers.add_parser("delete-feed", help="Delete a saved feed.")
  feed_delete.add_argument("feed_id")

  feed_run = subparsers.add_parser("run-feed", help="Run a saved feed.")
  feed_run.add_argument("feed_id")
  feed_run.add_argument("--url", default="")
  feed_run.add_argument("--max-items", type=int)
  feed_run.add_argument("--timeout-seconds", type=int)
  feed_run.add_argument("--raw-snapshots", action=argparse.BooleanOptionalAction, default=None)
  feed_run.add_argument("--wait", action="store_true")
  feed_run.add_argument("--poll-interval", type=float, default=2.0)
  feed_run.add_argument("--wait-timeout", type=float, default=0)

  feed_download = subparsers.add_parser("download-feed", help="Download latest outputs from a saved feed.")
  feed_download.add_argument("feed_id")
  feed_download.add_argument("--out", type=Path, required=True)

  schema_get = subparsers.add_parser("schema", help="Get one extraction schema.")
  schema_get.add_argument("schema_id")

  schema_save = subparsers.add_parser("save-schema", help="Create or update an extraction schema from JSON.")
  schema_save.add_argument("--file", type=Path, required=True)

  schema_delete = subparsers.add_parser("delete-schema", help="Delete an extraction schema.")
  schema_delete.add_argument("schema_id")

  submit = subparsers.add_parser("submit", help="Submit a URL scrape job.")
  submit.add_argument("url")
  submit.add_argument("--job-app-id", default="",
                      help="App id to assign when submitting with admin token.")
  submit.add_argument("--schema-id", default="")
  submit.add_argument("--schema-file", type=Path)
  submit.add_argument("--max-items", type=int)
  submit.add_argument("--timeout-seconds", type=int)
  submit.add_argument("--raw-snapshots", action=argparse.BooleanOptionalAction, default=None)
  submit.add_argument("--wait", action="store_true")
  submit.add_argument("--poll-interval", type=float, default=2.0)
  submit.add_argument("--wait-timeout", type=float, default=0,
                      help="Seconds to wait. 0 means no client-side wait limit.")

  status = subparsers.add_parser("status", help="Get one job.")
  status.add_argument("job_id")

  items = subparsers.add_parser("items", help="Get normalized items from one job.")
  items.add_argument("job_id")
  items.add_argument("--since", type=int, default=0)
  items.add_argument("--limit", type=int, default=0)

  events = subparsers.add_parser("events", help="Read or stream job events.")
  events.add_argument("job_id")
  event_transport = events.add_mutually_exclusive_group()
  event_transport.add_argument("--stream", action="store_true",
                               help="Use the SSE compatibility stream.")
  event_transport.add_argument("--websocket", action="store_true",
                               help="Use the low-latency app WebSocket.")
  events.add_argument("--since", default="latest")
  events.add_argument("--after-sequence", type=int,
                      help="Exact replay cursor (default: current sequence).")

  preview = subparsers.add_parser("preview-schema", help="Preview a schema against a live job tab.")
  preview.add_argument("job_id")
  preview.add_argument("--schema-id", default="")
  preview.add_argument("--schema-file", type=Path)

  control = subparsers.add_parser("control", help="Run a live tab control action.")
  control.add_argument("job_id")
  control.add_argument("action")
  control.add_argument("--selector", default="")
  control.add_argument("--text", default="")
  control.add_argument("--expression", default="")
  control.add_argument("--x", type=int, default=0)
  control.add_argument("--y", type=int, default=800)
  control.add_argument("--full-page", action="store_true")
  control.add_argument("--json", default="",
                       help="Extra action parameters as a JSON object.")

  download = subparsers.add_parser("download", help="Download known job output files.")
  download.add_argument("job_id")
  download.add_argument("--out", type=Path, required=True)

  for name in ("resume", "stop", "retry", "close-tab"):
    command = subparsers.add_parser(name)
    command.add_argument("job_id")

  subparsers.add_parser(
      "close-completed-tabs", help="Close all terminal job tabs in this app scope.")

  return parser.parse_args()


def main() -> int:
  args = parse_args()
  client = BrokerClient(
      broker_url=args.broker,
      token=args.token,
      app_id=args.app_id,
      app_secret=args.app_secret,
  )
  try:
    if args.command == "ensure-service":
      print_json(ensure_service(
          timeout_seconds=args.timeout_seconds,
          broker_url=args.broker,
          cdp_endpoint=args.cdp,
          state_dir=args.state_dir,
          output_root=args.output_root,
          no_auth=args.no_auth,
      ))
    elif args.command == "register-app":
      print_json(client.register_app(args.name, args.requested_app_id, args.description))
    elif args.command == "apps":
      print_json(client.list_apps())
    elif args.command == "jobs":
      print_json(client.list_jobs())
    elif args.command == "feeds":
      print_json(client.list_feeds())
    elif args.command == "feed":
      print_json(client.get_feed(args.feed_id))
    elif args.command == "save-feed":
      print_json(client.save_feed(load_json_file(args.file)))
    elif args.command == "delete-feed":
      print_json(client.delete_feed(args.feed_id))
    elif args.command == "run-feed":
      result = client.run_feed(
          args.feed_id,
          url=args.url,
          max_items=args.max_items,
          timeout_seconds=args.timeout_seconds,
          raw_snapshots=args.raw_snapshots,
      )
      if args.wait:
        result = wait_for_job(
            client,
            result["job"]["id"],
            args.poll_interval,
            args.wait_timeout,
        )
      print_json(result)
    elif args.command == "download-feed":
      written = download_feed_outputs(client, args.feed_id, args.out)
      print_json({"ok": True, "files": [str(path) for path in written]})
    elif args.command == "schemas":
      print_json(client.list_schemas())
    elif args.command == "schema":
      print_json(client.get_schema(args.schema_id))
    elif args.command == "save-schema":
      print_json(client.save_schema(load_json_file(args.file)))
    elif args.command == "delete-schema":
      print_json(client.delete_schema(args.schema_id))
    elif args.command == "submit":
      result = client.submit_job(
          args.url,
          app_id=args.job_app_id,
          schema_id=args.schema_id,
          schema=load_json_file(args.schema_file) if args.schema_file else None,
          max_items=args.max_items,
          timeout_seconds=args.timeout_seconds,
          raw_snapshots=args.raw_snapshots,
      )
      if args.wait:
        result = wait_for_job(
            client,
            result["job"]["id"],
            args.poll_interval,
            args.wait_timeout,
        )
      print_json(result)
    elif args.command == "status":
      print_json(client.get_job(args.job_id))
    elif args.command == "items":
      print_json(client.get_items(args.job_id, args.since, args.limit))
    elif args.command == "events":
      if args.websocket:
        after_sequence = args.after_sequence
        if after_sequence is None:
          job = client.get_job(args.job_id).get("job", {})
          after_sequence = int(job.get("eventSequence") or 0)
        for message in client.stream_app_events({args.job_id: after_sequence}):
          print_json(message)
      elif args.stream:
        for event, payload in client.stream_events(
            args.job_id, args.since, args.after_sequence):
          print_json({"event": event, "data": payload})
      else:
        print(client.event_log(args.job_id), end="")
    elif args.command == "preview-schema":
      print_json(client.preview_schema(
          args.job_id,
          schema=load_json_file(args.schema_file) if args.schema_file else None,
          schema_id=args.schema_id))
    elif args.command == "control":
      extra = json.loads(args.json) if args.json else {}
      if not isinstance(extra, dict):
        raise BrokerClientError("--json must be a JSON object")
      if args.selector:
        extra["selector"] = args.selector
      if args.text:
        extra["text"] = args.text
      if args.expression:
        extra["expression"] = args.expression
      if args.action == "scroll":
        extra.setdefault("x", args.x)
        extra.setdefault("y", args.y)
      if args.action == "screenshot":
        extra.setdefault("full_page", args.full_page)
      print_json(client.control_job(args.job_id, args.action, **extra))
    elif args.command == "download":
      written = download_outputs(client, args.job_id, args.out)
      print_json({"ok": True, "files": [str(path) for path in written]})
    elif args.command == "resume":
      print_json(client.resume_job(args.job_id))
    elif args.command == "stop":
      print_json(client.stop_job(args.job_id))
    elif args.command == "retry":
      print_json(client.retry_job(args.job_id))
    elif args.command == "close-tab":
      print_json(client.close_tab(args.job_id))
    elif args.command == "close-completed-tabs":
      print_json(client.close_completed_tabs())
    else:
      raise BrokerClientError(f"unknown command: {args.command}")
  except BrokerClientError as error:
    print(f"error: {error}", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
