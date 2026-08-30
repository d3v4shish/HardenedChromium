#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""On-demand supervisor for the local Hardened Chromium scrape stack.

This helper is intentionally dependency-free. It starts/reuses:

  * visible Hardened Chromium with CDP on loopback; and
  * hardened_scrape_broker.py with a stable local bearer token.

Other local apps can call `ensure --json` and then talk to the broker.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse
import urllib.error
import urllib.request


SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = SCRIPT_DIR.parent.parent
DEFAULT_CDP_ENDPOINT = "auto"
DEFAULT_BROKER_URL = "http://127.0.0.1:8877"
SERVICE_BACKEND_ID = "hardened-chromium-broker"
SERVICE_PROTOCOL_VERSION = 1
SERVICE_CAPABILITIES = (
    "browser-jobs",
    "extraction-schemas",
    "feed-generation",
    "websocket-events",
    "sse-events",
    "privacy-controls",
)
DEFAULT_STATE_DIR = Path.home() / ".local/state/hardened-chromium-scrape"
DEFAULT_OUTPUT_ROOT = Path.home() / "Downloads" / "Hardened Scrape Broker"
TERMINATE_TIMEOUT_SECONDS = 10
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
MEDIA_MODES = {"loop", "obs", "synthetic", "system", "none"}
AUDIO_CAPTURE_MODES = {"fake", "system"}
PRIVACY_SOURCES = {"fake", "real"}
FAKE_CAMERA_BACKENDS = {"loop", "obs", "synthetic"}
DEFAULT_FAKE_LOCATION = {
    "enabled": True,
    "latitude": 28.6139,
    "longitude": 77.2090,
    "accuracy": 100.0,
}
DEFAULT_LOOP_VIDEO_CANDIDATES = (
    Path.home() / "Workspace/Temp/Y4MConverter/asd.y4m",
)


class ServiceError(RuntimeError):
  def __init__(self, message: str, code: str = "service_error"):
    super().__init__(message)
    self.code = code


def default_loop_video_file() -> str:
  env_path = os.environ.get("HARDENED_DEFAULT_LOOP_VIDEO_FILE", "").strip()
  candidates: list[Path] = []
  if env_path:
    candidates.append(Path(env_path).expanduser())
  candidates.extend(DEFAULT_LOOP_VIDEO_CANDIDATES)
  for candidate in candidates:
    with contextlib.suppress(OSError):
      resolved = candidate.resolve()
      if resolved.is_file():
        return str(resolved)
  return ""


@dataclasses.dataclass(frozen=True)
class HttpProbe:
  ok: bool
  status: int | None = None
  payload: dict[str, Any] | None = None
  error: str = ""


@dataclasses.dataclass
class ServiceConfig:
  cdp_endpoint: str
  broker_url: str
  state_dir: Path
  output_root: Path
  launcher: Path
  broker_script: Path
  python: str
  profile: Path
  timeout_seconds: float
  no_auth: bool

  @property
  def token_file(self) -> Path:
    return self.state_dir / "token"

  @property
  def lock_file(self) -> Path:
    return self.state_dir / "service.lock"

  @property
  def state_file(self) -> Path:
    return self.state_dir / "state.json"

  @property
  def browser_pid_file(self) -> Path:
    return self.state_dir / "browser.pid"

  @property
  def broker_pid_file(self) -> Path:
    return self.state_dir / "broker.pid"

  @property
  def browser_log(self) -> Path:
    return self.state_dir / "browser.log"

  @property
  def broker_log(self) -> Path:
    return self.state_dir / "broker.log"

  @property
  def lifecycle_log(self) -> Path:
    return self.state_dir / "lifecycle.jsonl"

  @property
  def broker_state_dir(self) -> Path:
    return self.output_root / "_broker_state"

  @property
  def media_settings_file(self) -> Path:
    return self.broker_state_dir / "browser_media_settings.json"

  @property
  def location_settings_file(self) -> Path:
    return self.broker_state_dir / "browser_location_settings.json"


class ServiceLock:
  def __init__(self, path: Path):
    self.path = path
    self.file: Any = None

  def __enter__(self) -> "ServiceLock":
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.file = self.path.open("a+", encoding="utf-8")
    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
    return self

  def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
    if self.file:
      fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
      self.file.close()


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Start/reuse the local Hardened Chromium scrape service.")
  parser.add_argument("--cdp", default=os.environ.get(
      "HARDENED_CDP_ENDPOINT", DEFAULT_CDP_ENDPOINT))
  parser.add_argument("--broker", default=os.environ.get(
      "HARDENED_BROKER_URL", DEFAULT_BROKER_URL))
  parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get(
      "HARDENED_SCRAPE_STATE_DIR", str(DEFAULT_STATE_DIR))))
  parser.add_argument("--output-root", type=Path, default=Path(os.environ.get(
      "HARDENED_BROKER_OUTPUT_ROOT", str(DEFAULT_OUTPUT_ROOT))))
  parser.add_argument("--launcher", type=Path, default=Path(os.environ.get(
      "HARDENED_SCRAPE_BROWSER_LAUNCHER",
      str(SCRIPT_DIR / "run_for_automation.sh"))))
  parser.add_argument("--broker-script", type=Path, default=Path(os.environ.get(
      "HARDENED_SCRAPE_BROKER_SCRIPT",
      str(SCRIPT_DIR / "hardened_scrape_broker.py"))))
  parser.add_argument("--python", default=os.environ.get(
      "HARDENED_SCRAPE_PYTHON", sys.executable))
  parser.add_argument("--profile", type=Path, default=Path(os.environ.get(
      "HARDENED_AUTOMATION_PROFILE",
      str(SOURCE_DIR / "out/HardenedAutomationProfile"))))
  parser.add_argument("--timeout-seconds", type=float, default=90)
  parser.add_argument("--json", action="store_true",
                      help="Print machine-readable JSON.")
  parser.add_argument("--no-auth", action="store_true",
                      default=is_truthy_env("HARDENED_BROKER_NO_AUTH"),
                      help="Start/reuse broker in loopback-only no-auth mode.")

  subparsers = parser.add_subparsers(dest="command", required=True)
  for name in ("ensure", "start", "status", "stop", "restart",
               "diagnostics"):
    add_common_command_flags(subparsers.add_parser(name))
  add_common_command_flags(subparsers.add_parser(
      "capabilities", help="Describe the installed broker contract without starting it."))
  stop_browser = subparsers.add_parser("stop-browser")
  add_common_command_flags(stop_browser)
  stop_browser.add_argument(
      "--confirm-shared-browser", action="store_true",
      help="Confirm that every default-profile window may be closed.")
  logs = subparsers.add_parser("logs")
  add_common_command_flags(logs)
  logs.add_argument("--tail", type=int, default=12000,
                    help="Bytes to print from each log. 0 prints only paths.")
  return parser.parse_args()


def add_common_command_flags(parser: argparse.ArgumentParser) -> None:
  # Accept both `--json status` and `status --json` so users and helper
  # wrappers do not need to remember argparse's global-option ordering.
  parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
  parser.add_argument("--no-auth", action="store_true", default=argparse.SUPPRESS)
  parser.add_argument(
      "--timeout-seconds", type=float, default=argparse.SUPPRESS)


def build_config(args: argparse.Namespace) -> ServiceConfig:
  if args.cdp != "auto":
    validate_loopback_url(args.cdp, "CDP")
  validate_loopback_url(args.broker, "broker")
  return ServiceConfig(
      cdp_endpoint=args.cdp if args.cdp == "auto" else args.cdp.rstrip("/"),
      broker_url=args.broker.rstrip("/"),
      state_dir=args.state_dir.expanduser().resolve(),
      output_root=args.output_root.expanduser().resolve(),
      launcher=args.launcher.expanduser().resolve(),
      broker_script=args.broker_script.expanduser().resolve(),
      python=args.python,
      profile=args.profile.expanduser().resolve(),
      timeout_seconds=args.timeout_seconds,
      no_auth=args.no_auth,
  )


def validate_loopback_url(value: str, label: str) -> None:
  parsed = urlparse(value)
  host = parsed.hostname or ""
  if parsed.scheme != "http" or host not in LOOPBACK_HOSTS:
    raise ServiceError(
        f"{label} endpoint must be http on loopback, got {value!r}")


def is_truthy_env(name: str) -> bool:
  return os.environ.get(name, "").strip().lower() in (
      "1", "true", "yes", "on")


def ensure_state_dir(config: ServiceConfig) -> None:
  config.state_dir.mkdir(parents=True, exist_ok=True)
  with contextlib.suppress(OSError):
    config.state_dir.chmod(0o700)


def read_or_create_token(config: ServiceConfig) -> str:
  if config.no_auth:
    return ""
  ensure_state_dir(config)
  token = read_token(config)
  if token:
    with contextlib.suppress(OSError):
      config.token_file.chmod(0o600)
    return token
  token = secrets.token_urlsafe(32)
  fd = os.open(
      config.token_file,
      os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
      0o600)
  with os.fdopen(fd, "w", encoding="utf-8") as file:
    file.write(token + "\n")
  return token


def read_token(config: ServiceConfig) -> str:
  """Read the existing local token without creating state."""
  if config.no_auth:
    return ""
  try:
    return config.token_file.read_text(encoding="utf-8").strip()
  except OSError:
    return ""


def read_browser_media_settings(config: ServiceConfig) -> dict[str, Any]:
  settings: dict[str, Any] = {}
  if config.media_settings_file.exists():
    try:
      value = json.loads(config.media_settings_file.read_text(encoding="utf-8"))
      if isinstance(value, dict):
        settings = value
    except Exception:
      settings = {}

  media_mode = str(settings.get("mediaMode") or settings.get("media_mode") or "loop")
  if media_mode not in MEDIA_MODES:
    media_mode = "loop"
  camera_source = str(
      settings.get("cameraSource") or settings.get("camera_source") or
      ("real" if media_mode in ("system", "none") else "fake"))
  camera_source = camera_source.strip().lower()
  if camera_source not in PRIVACY_SOURCES:
    camera_source = "fake"
  fake_camera_backend = str(
      settings.get("fakeCameraBackend") or
      settings.get("fake_camera_backend") or
      (media_mode if media_mode in FAKE_CAMERA_BACKENDS else "loop"))
  fake_camera_backend = fake_camera_backend.strip().lower()
  if fake_camera_backend not in FAKE_CAMERA_BACKENDS:
    fake_camera_backend = "loop"
  media_mode = "system" if camera_source == "real" else fake_camera_backend
  audio_capture_mode = str(
      settings.get("audioCaptureMode") or settings.get("audio_capture_mode") or
      "fake").strip().lower()
  if audio_capture_mode not in AUDIO_CAPTURE_MODES:
    audio_capture_mode = "fake"
  microphone_source = str(
      settings.get("microphoneSource") or
      settings.get("microphone_source") or
      ("real" if audio_capture_mode == "system" else "fake"))
  microphone_source = microphone_source.strip().lower()
  if microphone_source not in PRIVACY_SOURCES:
    microphone_source = "fake"
  audio_capture_mode = (
      "system" if microphone_source == "real" else "fake")

  loop_video_file = str(
      settings.get("loopVideoFile") or settings.get("loop_video_file") or
      default_loop_video_file()).strip()
  loop_video_exists = bool(loop_video_file and Path(loop_video_file).exists())
  loop_video_name = str(settings.get("loopVideoName") or "")
  loop_video_bytes = int(settings.get("loopVideoBytes") or 0)
  if loop_video_exists:
    loop_video_path = Path(loop_video_file)
    loop_video_name = loop_video_name or loop_video_path.name
    with contextlib.suppress(OSError):
      loop_video_bytes = loop_video_path.stat().st_size
  return {
      "schemaVersion": 2,
      "cameraSource": camera_source,
      "fakeCameraBackend": fake_camera_backend,
      "microphoneSource": microphone_source,
      "mediaMode": media_mode,
      "audioCaptureMode": audio_capture_mode,
      "loopVideoFile": loop_video_file,
      "loopVideoName": loop_video_name,
      "loopVideoBytes": loop_video_bytes,
      "loopVideoExists": loop_video_exists,
      "settingsFile": str(config.media_settings_file),
      "updatedAt": str(settings.get("updatedAt") or ""),
  }


def bool_value(value: Any, default: bool = False) -> bool:
  if isinstance(value, bool):
    return value
  if value is None:
    return default
  text = str(value).strip().lower()
  if text in ("1", "true", "yes", "on", "enabled"):
    return True
  if text in ("0", "false", "no", "off", "disabled"):
    return False
  return default


def clamp_float(value: Any, minimum: float, maximum: float,
                default: float) -> float:
  try:
    number = float(value)
  except (TypeError, ValueError):
    number = default
  return max(minimum, min(maximum, number))


def read_browser_location_settings(config: ServiceConfig) -> dict[str, Any]:
  settings: dict[str, Any] = {}
  if config.location_settings_file.exists():
    try:
      value = json.loads(config.location_settings_file.read_text(encoding="utf-8"))
      if isinstance(value, dict):
        settings = value
    except Exception:
      settings = {}
  source = str(
      settings.get("source") or settings.get("locationSource") or
      ("fake" if bool_value(
          settings.get("enabled"), DEFAULT_FAKE_LOCATION["enabled"])
       else "real")).strip().lower()
  if source not in PRIVACY_SOURCES:
    source = "fake"
  return {
      "schemaVersion": 2,
      "source": source,
      "enabled": source == "fake",
      "latitude": clamp_float(
          settings.get("latitude"), -90.0, 90.0,
          DEFAULT_FAKE_LOCATION["latitude"]),
      "longitude": clamp_float(
          settings.get("longitude"), -180.0, 180.0,
          DEFAULT_FAKE_LOCATION["longitude"]),
      "accuracy": clamp_float(
          settings.get("accuracy"), 1.0, 100000.0,
          DEFAULT_FAKE_LOCATION["accuracy"]),
      "settingsFile": str(config.location_settings_file),
      "updatedAt": str(settings.get("updatedAt") or ""),
      "restartRequired": True,
  }


def http_json(
    url: str,
    token: str = "",
    timeout: float = 2,
) -> HttpProbe:
  headers = {}
  if token:
    headers["Authorization"] = f"Bearer {token}"
  request = urllib.request.Request(url, headers=headers)
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      raw = response.read()
      payload = json.loads(raw.decode("utf-8"))
      return HttpProbe(
          ok=200 <= response.status < 300,
          status=response.status,
          payload=payload if isinstance(payload, dict) else {"value": payload},
      )
  except urllib.error.HTTPError as error:
    detail = error.read().decode("utf-8", "replace")
    return HttpProbe(ok=False, status=error.code, error=detail or error.reason)
  except Exception as error:  # pylint: disable=broad-except
    return HttpProbe(ok=False, error=str(error))


def discover_cdp_endpoint(config: ServiceConfig) -> str:
  """Resolve Chromium's private ephemeral DevTools endpoint."""
  if config.cdp_endpoint != "auto":
    return config.cdp_endpoint
  active_port_file = config.profile / "DevToolsActivePort"
  try:
    lines = active_port_file.read_text(encoding="utf-8").splitlines()
    port = int(lines[0].strip())
  except (OSError, ValueError, IndexError):
    return ""
  if not 1 <= port <= 65535:
    return ""
  return f"http://127.0.0.1:{port}"


def cdp_probe(config: ServiceConfig) -> HttpProbe:
  endpoint = discover_cdp_endpoint(config)
  if not endpoint:
    return HttpProbe(False, error="DevToolsActivePort is not available")
  return http_json(endpoint + "/json/version", timeout=2)


def broker_probe(config: ServiceConfig, token: str) -> HttpProbe:
  probe = http_json(config.broker_url + "/health", token=token, timeout=2)
  if not probe.ok:
    return probe
  payload = probe.payload or {}
  if payload.get("service") != "hardened-scrape-broker":
    return HttpProbe(
        False, status=probe.status,
        error="listener is not a Hardened Scrape Broker")
  if Path(str(payload.get("outputRoot") or "")).expanduser() != config.output_root:
    return HttpProbe(
        False, status=probe.status,
        error="broker is using a different output root")
  expected_auth = "none" if config.no_auth else "token"
  if payload.get("auth") != expected_auth:
    return HttpProbe(
        False, status=probe.status,
        error="broker authentication mode does not match service settings")
  return probe


def endpoint_host_port(url: str, default_port: int) -> tuple[str, int]:
  parsed = urlparse(url)
  return parsed.hostname or "127.0.0.1", parsed.port or default_port


def read_pid(path: Path) -> int | None:
  try:
    value = int(path.read_text(encoding="utf-8").strip())
  except Exception:
    return None
  return value if value > 0 else None


def write_pid(path: Path, pid: int) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(f"{pid}\n", encoding="utf-8")


def pid_alive(pid: int | None) -> bool:
  if not pid:
    return False
  try:
    os.kill(pid, 0)
    return True
  except ProcessLookupError:
    return False
  except PermissionError:
    return True
  # os.kill(pid, 0) succeeds for a zombie until its parent reaps it. A failed
  # launcher must not keep the supervisor in its readiness loop for 90s.
  try:
    stat_fields = Path(f"/proc/{pid}/stat").read_text(
        encoding="utf-8").rsplit(")", 1)[1].split()
    return bool(stat_fields) and stat_fields[0] != "Z"
  except (OSError, IndexError):
    return True


def proc_cmdline(pid: int) -> str:
  try:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
  except Exception:
    return ""
  return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def process_matches(pid: int | None, markers: list[str]) -> bool:
  if not pid_alive(pid):
    return False
  command = proc_cmdline(pid or 0)
  return any(marker and marker in command for marker in markers)


def profile_locked_by_other_browser(config: ServiceConfig) -> bool:
  # Chromium can leave a broken SingletonLock link after a crash or normal
  # close. Only a verified live process using this exact profile is a reason
  # to refuse recovery.
  return profile_browser_pid(config) is not None


def clear_stale_browser_runtime_files(config: ServiceConfig) -> None:
  """Remove Chromium runtime markers only after ownership was ruled out."""
  if profile_browser_pid(config) is not None:
    return
  for name in (
      "SingletonLock",
      "SingletonCookie",
      "SingletonSocket",
      "DevToolsActivePort",
  ):
    path = config.profile / name
    try:
      # exists() is false for a broken symlink, while is_symlink() is true.
      if path.exists() or path.is_symlink():
        path.unlink()
    except OSError:
      # Chromium will perform its own profile-lock check during launch. A
      # marker we cannot remove is not a reason to touch unrelated files.
      pass


def append_lifecycle_event(
    config: ServiceConfig,
    event: str,
    **details: Any,
) -> None:
  """Append a compact, secret-free supervisor event.

  This file is deliberately separate from the browser and broker stderr logs:
  it gives support a reliable timeline for app-triggered starts and recovery
  without requiring a caller to expose its bearer token or request URL.
  """
  record = {
      "timeEpoch": time.time(),
      "event": event,
      "supervisorPid": os.getpid(),
      "requestingProcessPid": os.getppid(),
      **details,
  }
  try:
    config.lifecycle_log.parent.mkdir(parents=True, exist_ok=True)
    with config.lifecycle_log.open("a", encoding="utf-8") as file:
      file.write(json.dumps(record, ensure_ascii=False) + "\n")
  except OSError:
    # Observability must never prevent a recovery from starting the browser.
    pass


def start_browser(config: ServiceConfig) -> int:
  if not config.launcher.exists():
    raise ServiceError(f"browser launcher not found: {config.launcher}")
  if profile_locked_by_other_browser(config):
    endpoint_description = (
        config.cdp_endpoint if config.cdp_endpoint != "auto"
        else "the profile's private CDP endpoint")
    raise ServiceError(
        f"{config.profile} appears to be open, but CDP is not reachable at "
        f"{endpoint_description}. Close that Chromium profile or restart it "
        "with the automation launcher.", code="browser_unreachable")
  clear_stale_browser_runtime_files(config)

  env = os.environ.copy()
  if config.cdp_endpoint == "auto":
    env["HARDENED_REMOTE_DEBUGGING_ADDRESS"] = "127.0.0.1"
    env["HARDENED_REMOTE_DEBUGGING_PORT"] = "0"
  else:
    host, port = endpoint_host_port(config.cdp_endpoint, 80)
    env["HARDENED_REMOTE_DEBUGGING_ADDRESS"] = host
    env["HARDENED_REMOTE_DEBUGGING_PORT"] = str(port)
  env["HARDENED_AUTOMATION_PROFILE"] = str(config.profile)
  media_settings = read_browser_media_settings(config)
  env["HARDENED_MEDIA_MODE"] = str(media_settings["mediaMode"])
  env["HARDENED_AUDIO_CAPTURE_MODE"] = str(media_settings["audioCaptureMode"])
  env["HARDENED_CAMERA_SOURCE"] = str(media_settings["cameraSource"])
  env["HARDENED_MICROPHONE_SOURCE"] = str(
      media_settings["microphoneSource"])
  if media_settings["mediaMode"] == "loop" and media_settings["loopVideoFile"]:
    env["HARDENED_LOOP_VIDEO_FILE"] = str(media_settings["loopVideoFile"])
  location_settings = read_browser_location_settings(config)
  env["HARDENED_LOCATION_SOURCE"] = str(location_settings["source"])
  env["HARDENED_FAKE_LOCATION_LATITUDE"] = str(
      location_settings["latitude"])
  env["HARDENED_FAKE_LOCATION_LONGITUDE"] = str(
      location_settings["longitude"])
  env["HARDENED_FAKE_LOCATION_ACCURACY"] = str(
      location_settings["accuracy"])
  env["HARDENED_PRIVACY_RULES_FILE"] = str(
      config.broker_state_dir / "browser_privacy_rules.json")
  config.browser_log.parent.mkdir(parents=True, exist_ok=True)
  append_lifecycle_event(
      config, "browser_starting", cdpMode=config.cdp_endpoint,
      profile=str(config.profile))
  browser_log = config.browser_log.open("ab", buffering=0)
  try:
    process = subprocess.Popen(
        [str(config.launcher)],
        stdin=subprocess.DEVNULL,
        stdout=browser_log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
  finally:
    browser_log.close()
  write_pid(config.browser_pid_file, process.pid)
  append_lifecycle_event(config, "browser_started", browserPid=process.pid)
  return process.pid


def start_broker(config: ServiceConfig, token: str) -> int:
  if not config.broker_script.exists():
    raise ServiceError(f"broker script not found: {config.broker_script}")
  host, port = endpoint_host_port(config.broker_url, 8877)
  config.broker_log.parent.mkdir(parents=True, exist_ok=True)
  broker_log = config.broker_log.open("ab", buffering=0)
  cdp_endpoint = discover_cdp_endpoint(config)
  if not cdp_endpoint:
    broker_log.close()
    raise ServiceError("cannot start broker before private CDP is ready")
  append_lifecycle_event(config, "broker_starting", brokerUrl=config.broker_url)
  try:
    process = subprocess.Popen(
        broker_command(config, token, host, port, cdp_endpoint),
        stdin=subprocess.DEVNULL,
        stdout=broker_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
  finally:
    broker_log.close()
  write_pid(config.broker_pid_file, process.pid)
  append_lifecycle_event(config, "broker_started", brokerPid=process.pid)
  return process.pid


def broker_command(
    config: ServiceConfig,
    token: str,
    host: str,
    port: int,
    cdp_endpoint: str,
) -> list[str]:
  command = [
      config.python,
      str(config.broker_script),
      "--cdp",
      cdp_endpoint,
      "--root",
      str(config.output_root),
      "--host",
      host,
      "--port",
      str(port),
  ]
  if config.no_auth:
    command.append("--no-auth")
  else:
    command.extend(["--token", token])
  return command


def wait_for_probe(
    probe_function: Any,
    timeout_seconds: float,
    label: str,
    startup_pid: int | None = None,
) -> HttpProbe:
  deadline = time.monotonic() + timeout_seconds
  last = HttpProbe(False, error="not checked")
  while time.monotonic() < deadline:
    last = probe_function()
    if last.ok:
      return last
    if startup_pid is not None and not pid_alive(startup_pid):
      raise ServiceError(f"{label} exited before becoming ready")
    time.sleep(0.5)
  raise ServiceError(f"{label} did not become ready: {last.error or last.status}")


def ensure_service(config: ServiceConfig) -> dict[str, Any]:
  ensure_state_dir(config)
  with ServiceLock(config.lock_file):
    token = read_or_create_token(config)
    append_lifecycle_event(config, "ensure_requested", brokerUrl=config.broker_url)

    # Resolve an incompatible listener before starting or changing the browser.
    # Callers can then ask the user before choosing the explicit restart action.
    broker = broker_probe(config, token)
    if not broker.ok and broker.status in (401, 403):
      if config.no_auth:
        raise ServiceError(
            f"An authenticated broker is already listening at "
            f"{config.broker_url}. Stop it or choose a different broker port "
            "before using no-auth mode.",
            code="auth_mode_conflict")
      raise ServiceError(
          f"A broker is already listening at {config.broker_url}, but it did "
          "not accept this service token.")
    if not broker.ok and broker.status is not None:
      raise ServiceError(
          f"An incompatible listener is already running at "
          f"{config.broker_url}: {broker.error or broker.status}",
          code="broker_conflict")

    cdp = cdp_probe(config)
    browser_started = False
    if not cdp.ok:
      append_lifecycle_event(config, "browser_recovery_requested")
      browser_pid = start_browser(config)
      browser_started = True
      cdp = wait_for_probe(
          lambda: cdp_probe(config), config.timeout_seconds, "CDP",
          startup_pid=browser_pid)
      append_lifecycle_event(config, "browser_ready")

    # The browser uses an ephemeral port. If it was recreated, an existing
    # owned broker necessarily points at the previous browser instance.
    if browser_started and broker.ok:
      broker_pid = read_pid(config.broker_pid_file)
      stopped = terminate_owned_pid(
          broker_pid,
          ["hardened_scrape_broker.py", str(config.output_root)],
          "broker")
      if stopped not in ("terminated", "killed", "not_running"):
        raise ServiceError(
            "the browser restarted but the existing broker is not owned by "
            "this service; stop that broker before ensuring the service",
            code="broker_ownership_conflict")
      with contextlib.suppress(OSError):
        config.broker_pid_file.unlink()
      broker = HttpProbe(False, error="browser backend changed")
      append_lifecycle_event(
          config, "broker_recycled", reason="browser_backend_recreated",
          result=stopped)

    broker_started = False
    if not broker.ok:
      broker_pid = start_broker(config, token)
      broker_started = True
      broker = wait_for_probe(
          lambda: broker_probe(config, token), config.timeout_seconds,
          "broker", startup_pid=broker_pid)
      append_lifecycle_event(config, "broker_ready")

    state = status_document(config, token, cdp, broker)
    state["started"] = {
        "browser": browser_started,
        "broker": broker_started,
    }
    atomic_write_json(config.state_file, state)
    append_lifecycle_event(
        config, "ensure_completed", browserStarted=browser_started,
        brokerStarted=broker_started)
    return state


def capabilities_document(config: ServiceConfig) -> dict[str, Any]:
  """Return the public contract without touching runtime state or CDP."""
  expected_binary = Path(os.environ.get(
      "HARDENED_CHROMIUM_BINARY", str(SOURCE_DIR / "out/Hardened/chrome")))
  required = {
      "launcher": config.launcher,
      "brokerScript": config.broker_script,
      "chromiumBinary": expected_binary,
  }
  missing = [name for name, path in required.items() if not path.exists()]
  command = os.environ.get(
      "HARDENED_CHROMIUM_SERVICE_COMMAND", Path(sys.argv[0]).name)
  return {
      "ok": not missing,
      "backend": SERVICE_BACKEND_ID,
      "protocolVersion": SERVICE_PROTOCOL_VERSION,
      "capabilities": list(SERVICE_CAPABILITIES),
      "command": command,
      "installation": {
          "sourceRoot": str(SOURCE_DIR),
          "launcher": str(config.launcher),
          "brokerScript": str(config.broker_script),
          "chromiumBinary": str(expected_binary),
          "missing": missing,
      },
  }


def status_document(
    config: ServiceConfig,
    token: str,
    cdp: HttpProbe | None = None,
    broker: HttpProbe | None = None,
) -> dict[str, Any]:
  cdp = cdp if cdp is not None else cdp_probe(config)
  broker = broker if broker is not None else broker_probe(config, token)
  browser_pid = profile_browser_pid(config)
  broker_pid = read_pid(config.broker_pid_file)
  return {
      "ok": cdp.ok and broker.ok,
      "auth": "none" if config.no_auth else "token",
      "cdp": {
          "ready": cdp.ok,
          "status": cdp.status,
          "error": cdp.error,
      },
      "broker": {
          "url": config.broker_url,
          "ready": broker.ok,
          "status": broker.status,
          "error": broker.error,
      },
      "token": token,
      "stateDir": str(config.state_dir),
      "tokenFile": None if config.no_auth else str(config.token_file),
      "outputRoot": str(config.output_root),
      "mediaSettings": read_browser_media_settings(config),
      "locationSettings": read_browser_location_settings(config),
      "logs": {
          "browser": str(config.browser_log),
          "broker": str(config.broker_log),
          "lifecycle": str(config.lifecycle_log),
          "brokerEvents": str(config.broker_state_dir / "events.jsonl"),
      },
      "pids": {
          "browser": browser_pid,
          "browserAlive": pid_alive(browser_pid),
          "broker": broker_pid,
          "brokerAlive": pid_alive(broker_pid),
      },
  }


def stop_service(config: ServiceConfig) -> dict[str, Any]:
  ensure_state_dir(config)
  with ServiceLock(config.lock_file):
    stopped: dict[str, str] = {}
    broker_pid = read_pid(config.broker_pid_file)
    stopped["broker"] = terminate_owned_pid(
        broker_pid,
        ["hardened_scrape_broker.py", str(config.output_root)],
        "broker")
    for path in (config.broker_pid_file,):
      with contextlib.suppress(OSError):
        path.unlink()
    token = read_or_create_token(config)
    state = status_document(config, token)
    state["ok"] = True
    state["stopped"] = stopped
    atomic_write_json(config.state_file, state)
    return state


def profile_browser_pid(config: ServiceConfig) -> int | None:
  recorded = read_pid(config.browser_pid_file)
  profile_marker = f"--user-data-dir={config.profile}"
  if process_matches(recorded, [profile_marker]):
    return recorded
  singleton_lock = config.profile / "SingletonLock"
  try:
    target = os.readlink(singleton_lock)
    candidate = int(target.rsplit("-", 1)[1])
  except (OSError, ValueError, IndexError):
    candidate = None
  if process_matches(candidate, [profile_marker]):
    return candidate

  # SingletonLock and the pid file can both be stale after a renderer or
  # launcher failure. Before creating another visible browser, verify whether
  # a live main browser already owns this exact profile. Child processes have
  # --type=; excluding them prevents a surviving renderer from blocking safe
  # recovery after its browser process is gone.
  try:
    process_entries = Path("/proc").iterdir()
    for entry in process_entries:
      if not entry.name.isdigit():
        continue
      pid = int(entry.name)
      command = proc_cmdline(pid)
      if (profile_marker in command and "--type=" not in command and
          "chrome" in Path(command.split(" ", 1)[0]).name):
        return pid
  except OSError:
    pass
  return None


def stop_shared_browser(config: ServiceConfig, confirmed: bool) -> dict[str, Any]:
  if not confirmed:
    raise ServiceError(
        "refusing to close the shared browser without "
        "--confirm-shared-browser",
        code="confirmation_required")
  stop_service(config)
  with ServiceLock(config.lock_file):
    browser_pid = profile_browser_pid(config)
    stopped = terminate_owned_pid(
        browser_pid,
        [f"--user-data-dir={config.profile}"],
        "shared_browser")
    with contextlib.suppress(OSError):
      config.browser_pid_file.unlink()
    token = read_or_create_token(config)
    state = status_document(config, token)
    state["ok"] = stopped in ("terminated", "killed", "not_running", "no_pid")
    state["stopped"] = {"browser": stopped}
    atomic_write_json(config.state_file, state)
    return state


def terminate_owned_pid(pid: int | None, markers: list[str], label: str) -> str:
  if not pid:
    return "no_pid"
  if not pid_alive(pid):
    return "not_running"
  command = proc_cmdline(pid)
  if not command or not all(marker and marker in command for marker in markers):
    return f"refused_unrecognized_process: {command}"
  os.kill(pid, signal.SIGTERM)
  deadline = time.monotonic() + TERMINATE_TIMEOUT_SECONDS
  while time.monotonic() < deadline:
    if not pid_alive(pid):
      return "terminated"
    time.sleep(0.2)
  os.kill(pid, signal.SIGKILL)
  return "killed"


def logs_document(config: ServiceConfig, tail: int) -> dict[str, Any]:
  value: dict[str, Any] = {
      "ok": True,
      "logs": {
          "browser": str(config.browser_log),
          "broker": str(config.broker_log),
          "lifecycle": str(config.lifecycle_log),
          "brokerEvents": str(config.broker_state_dir / "events.jsonl"),
      },
  }
  if tail > 0:
    value["tail"] = {
          "browser": tail_file(config.browser_log, tail),
          "broker": tail_file(config.broker_log, tail),
          "lifecycle": tail_file(config.lifecycle_log, tail),
          "brokerEvents": tail_file(config.broker_state_dir / "events.jsonl", tail),
    }
  return value


def jsonl_tail(path: Path, limit: int = 40) -> list[dict[str, Any]]:
  lines = tail_file(path, 64 * 1024).splitlines()
  records: list[dict[str, Any]] = []
  for line in lines:
    with contextlib.suppress(json.JSONDecodeError):
      record = json.loads(line)
      if isinstance(record, dict):
        records.append(record)
  return records[-limit:]


def browser_crash_indicators(path: Path, limit: int = 20) -> list[str]:
  markers = ("FATAL:", "Check failed:", "DCHECK failed:",
             "Received signal ", "Segmentation fault", "Aborted")
  return [redact_sensitive_text(line)
          for line in tail_file(path, 256 * 1024).splitlines()
          if any(marker in line for marker in markers)][-limit:]


def redact_sensitive_text(value: str) -> str:
  for name in ("token", "ticket"):
    marker = f"{name}="
    if marker not in value:
      continue
    prefix, suffix = value.split(marker, 1)
    delimiter = next((candidate for candidate in ("&", " ", "\t")
                      if candidate in suffix), "")
    if delimiter:
      _, remainder = suffix.split(delimiter, 1)
      value = f"{prefix}{marker}REDACTED{delimiter}{remainder}"
    else:
      value = f"{prefix}{marker}REDACTED"
  return value


def app_tab_activity(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
  activity: list[dict[str, Any]] = []
  for record in records:
    event_type = record.get("type")
    if event_type not in (
        "job_accepted", "tab_opening", "tab_opened", "tab_open_failed"):
      continue
    data = record.get("data")
    data = data if isinstance(data, dict) else {}
    safe_data = {
        key: redact_sensitive_text(str(data[key]))
        for key in ("host", "targetId", "error") if key in data
    }
    activity.append({
        "time": record.get("time"),
        "type": event_type,
        "jobId": record.get("jobId"),
        "appId": record.get("appId"),
        "data": safe_data,
    })
  return activity


def diagnostics_document(config: ServiceConfig, token: str) -> dict[str, Any]:
  """Return the app-open and browser-crash evidence without log megabytes."""
  status = status_document(config, token)
  # The service token is needed internally for the broker probe, but a
  # diagnostic report is commonly attached to a bug report and must not carry
  # an authentication credential.
  public_status = dict(status)
  public_status.pop("token", None)
  activity = app_tab_activity(jsonl_tail(
      config.broker_state_dir / "events.jsonl"))
  return {
      "ok": True,
      "status": public_status,
      "recentLifecycle": jsonl_tail(config.lifecycle_log),
      "recentAppTabActivity": activity[-40:],
      "browserCrashIndicators": browser_crash_indicators(config.browser_log),
      "logs": public_status["logs"],
  }


def tail_file(path: Path, limit: int) -> str:
  if not path.exists():
    return ""
  with path.open("rb") as file:
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(max(0, size - limit), os.SEEK_SET)
    return file.read().decode("utf-8", "replace")


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
  temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
  temporary.replace(path)


def print_result(value: dict[str, Any], json_output: bool) -> None:
  if json_output:
    print(json.dumps(value, indent=2, ensure_ascii=False))
    return
  print(f"ok: {value.get('ok')}")
  if "backend" in value:
    print(f"backend: {value['backend']} (protocol {value.get('protocolVersion')})")
    print("capabilities: " + ", ".join(value.get("capabilities", [])))
  if "cdp" in value:
    print(f"shared browser backend ready={value['cdp']['ready']}")
  if "broker" in value:
    print(f"broker: {value['broker']['url']} ready={value['broker']['ready']}")
  if "stateDir" in value:
    print(f"state: {value['stateDir']}")
  if "logs" in value:
    print(f"browser log: {value['logs']['browser']}")
    print(f"broker log: {value['logs']['broker']}")
  if "stopped" in value:
    print(f"stopped: {value['stopped']}")
  if "started" in value:
    print(f"started: {value['started']}")


def main() -> int:
  args = parse_args()
  try:
    config = build_config(args)
    if args.command == "capabilities":
      result = capabilities_document(config)
    elif args.command in ("ensure", "start"):
      result = ensure_service(config)
    elif args.command == "status":
      token = read_token(config)
      result = status_document(config, token)
    elif args.command == "diagnostics":
      token = read_token(config)
      result = diagnostics_document(config, token)
    elif args.command == "stop":
      result = stop_service(config)
    elif args.command == "restart":
      stop_service(config)
      result = ensure_service(config)
    elif args.command == "stop-browser":
      result = stop_shared_browser(config, args.confirm_shared_browser)
    elif args.command == "logs":
      result = logs_document(config, args.tail)
    else:
      raise ServiceError(f"unknown command: {args.command}")
    print_result(result, args.json)
    return 0 if result.get("ok", True) else 1
  except ServiceError as error:
    with contextlib.suppress(UnboundLocalError):
      append_lifecycle_event(config, "command_failed", command=args.command,
                             code=error.code)
    payload = {"ok": False, "code": error.code, "error": str(error)}
    print_result(payload, args.json)
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
