#!/bin/bash

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_directory="$(cd "${script_directory}/../.." && pwd)"
source "${script_directory}/performance_binary.sh"
chromium_binary="$(resolve_hardened_chromium_binary "${source_directory}")"
named_profile="${HARDENED_CHROMIUM_NAMED_PROFILE:-}"
if [[ "${1:-}" == "--named-profile" ]]; then
  if [[ $# -lt 2 ]]; then
    echo "--named-profile requires a name" >&2
    exit 2
  fi
  named_profile="$2"
  shift 2
fi
if [[ -n "${named_profile}" && ! "${named_profile}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Named profiles may contain only letters, digits, dot, underscore, and dash." >&2
  exit 2
fi

backend_capable=1
automation_profile="${HARDENED_AUTOMATION_PROFILE:-${source_directory}/out/HardenedAutomationProfile}"
if [[ -n "${named_profile}" ]]; then
  backend_capable=0
  automation_profile="${HARDENED_CHROMIUM_NAMED_PROFILE_ROOT:-${source_directory}/out/HardenedProfiles}/${named_profile}"
fi
profile_directory="${HARDENED_CHROMIUM_PROFILE_DIRECTORY:-Default}"
remote_debugging_address="${HARDENED_REMOTE_DEBUGGING_ADDRESS:-127.0.0.1}"
# Chromium treats port 0 as an automation launch and exposes
# navigator.webdriver. Keep CDP local but use a non-zero configurable port.
remote_debugging_port="${HARDENED_REMOTE_DEBUGGING_PORT:-9222}"
website_view_file="${HARDENED_WEBSITE_VIEW_FILE:-${HARDENED_PRIVACY_RULES_FILE:-${automation_profile}/HardenedWebsiteView.json}}"
webdriver_mode="${HARDENED_WEBDRIVER_MODE:-}"
if [[ -z "${webdriver_mode}" ]]; then
  webdriver_mode="$(PYTHONPATH="${script_directory}" python3 -c '
from pathlib import Path
from hardened_website_view import load_document
policy = load_document(Path(__import__("sys").argv[1]))["default"]
print(policy["exposures"].get("automation", "hide"))
' "${website_view_file}" 2>/dev/null || true)"
  webdriver_mode="${webdriver_mode:-hide}"
fi
sandbox_helper="${CHROME_DEVEL_SANDBOX:-/usr/local/sbin/chrome-devel-sandbox}"
wm_class="${HARDENED_CHROMIUM_WM_CLASS:-HardenedChromium}"
media_mode="${HARDENED_MEDIA_MODE:-loop}"
audio_capture_mode="${HARDENED_AUDIO_CAPTURE_MODE:-fake}"
camera_source="${HARDENED_CAMERA_SOURCE:-}"
microphone_source="${HARDENED_MICROPHONE_SOURCE:-}"
obs_command="${HARDENED_OBS_COMMAND:-obs}"
obs_camera_name="${HARDENED_OBS_CAMERA_NAME:-OBS Virtual Camera}"
obs_log="${HARDENED_OBS_LOG:-/tmp/hardened-chromium-obs.log}"
loop_video_file="${HARDENED_LOOP_VIDEO_FILE:-${source_directory}/out/HardenedVirtualMedia/loop.y4m}"
loop_video_width="${HARDENED_LOOP_VIDEO_WIDTH:-320}"
loop_video_height="${HARDENED_LOOP_VIDEO_HEIGHT:-180}"
loop_video_frames="${HARDENED_LOOP_VIDEO_FRAMES:-120}"
loop_video_fps="${HARDENED_LOOP_VIDEO_FPS:-30}"
# Chromium reports that its Wayland backend is incompatible with Vulkan on
# this platform. When presentation feedback stops arriving, renderer callback
# queues grow until Chromium intentionally DCHECKs. Prefer the stable GL path;
# advanced users can opt back in after validating their driver stack.
disable_vulkan="${HARDENED_DISABLE_VULKAN:-1}"

if [[ ! -x "${chromium_binary}" ]]; then
  echo "Hardened Chromium binary not found: ${chromium_binary}" >&2
  exit 1
fi

if [[ ! -x "${sandbox_helper}" ]]; then
  echo "Chromium sandbox helper not found: ${sandbox_helper}" >&2
  echo "Install the compiled helper as described in the project README." >&2
  exit 1
fi

mkdir -p "${automation_profile}"
chmod 700 "${automation_profile}" 2>/dev/null || true

if [[ "${remote_debugging_address}" != "127.0.0.1" &&
      "${remote_debugging_address}" != "::1" &&
      "${HARDENED_ALLOW_NON_LOOPBACK_DEBUGGING:-0}" != "1" ]]; then
  echo "Refusing to expose remote debugging on ${remote_debugging_address}." >&2
  echo "Use HARDENED_ALLOW_NON_LOOPBACK_DEBUGGING=1 only on a trusted network." >&2
  exit 1
fi

if ! [[ "${remote_debugging_port}" =~ ^[1-9][0-9]*$ ]] ||
    (( remote_debugging_port > 65535 )); then
  echo "HARDENED_REMOTE_DEBUGGING_PORT must be a non-zero TCP port." >&2
  exit 1
fi

if [[ "${webdriver_mode}" != "hide" && "${webdriver_mode}" != "report" ]]; then
  echo "Unknown HARDENED_WEBDRIVER_MODE=${webdriver_mode}; expected hide or report." >&2
  exit 1
fi

backend_flags=()
backend_lock_held=0
if [[ "${backend_capable}" == "1" ]]; then
  runtime_root="${XDG_RUNTIME_DIR:-/tmp/hardened-chromium-${UID}}"
  backend_runtime_directory="${HARDENED_CHROMIUM_RUNTIME_DIR:-${runtime_root}/hardened-chromium}"
  mkdir -p "${backend_runtime_directory}"
  chmod 700 "${backend_runtime_directory}" 2>/dev/null || true
  backend_lock="${backend_runtime_directory}/backend.lock"
  exec 9>"${backend_lock}"
  if flock -n 9; then
    backend_lock_held=1
    backend_flags+=(
      --remote-debugging-address="${remote_debugging_address}"
      --remote-debugging-port="${remote_debugging_port}"
    )
  else
    # Do not let a simultaneous second launch win Chromium's profile singleton
    # race before the backend-holding launcher has bound its fixed CDP port.
    backend_ready=0
    for _ in {1..150}; do
      backend_host_header="${remote_debugging_address}"
      if [[ "${backend_host_header}" == "::1" ]]; then
        backend_host_header="[::1]"
      fi
      if [[ "${remote_debugging_port}" =~ ^[1-9][0-9]*$ ]] &&
         (exec 8<>"/dev/tcp/${remote_debugging_address}/${remote_debugging_port}" &&
          printf 'GET /json/version HTTP/1.0\r\nHost: %s\r\n\r\n' "${backend_host_header}" >&8 &&
          IFS= read -r backend_status <&8 &&
          [[ "${backend_status}" == *" 200 "* ]]) 2>/dev/null; then
        backend_ready=1
        break
      fi
      if flock -n 9; then
        backend_lock_held=1
        backend_flags+=(
          --remote-debugging-address="${remote_debugging_address}"
          --remote-debugging-port="${remote_debugging_port}"
        )
        break
      fi
      sleep 0.1
    done
    if [[ "${backend_lock_held}" == "0" && "${backend_ready}" == "0" ]]; then
      echo "Shared backend launcher holds the lock but did not become ready." >&2
      exit 1
    fi
    if [[ "${backend_lock_held}" == "0" ]]; then
      echo "Shared Hardened Chromium backend is already running; opening another window." >&2
    fi
  fi
fi

extra_flags=()
if [[ -n "${HARDENED_REMOTE_ALLOW_ORIGINS:-}" ]]; then
  extra_flags+=(--remote-allow-origins="${HARDENED_REMOTE_ALLOW_ORIGINS}")
fi
if [[ "${disable_vulkan}" != "0" ]]; then
  extra_flags+=(--disable-vulkan)
fi

if [[ "${audio_capture_mode}" != "fake" && "${audio_capture_mode}" != "system" ]]; then
  echo "Unknown HARDENED_AUDIO_CAPTURE_MODE=${audio_capture_mode}; expected fake or system." >&2
  exit 1
fi

if [[ -z "${camera_source}" ]]; then
  camera_source="fake"
  [[ "${media_mode}" == "system" || "${media_mode}" == "none" ]] && camera_source="real"
fi
if [[ -z "${microphone_source}" ]]; then
  microphone_source="fake"
  [[ "${audio_capture_mode}" == "system" ]] && microphone_source="real"
fi
if [[ "${camera_source}" != "fake" && "${camera_source}" != "real" ]]; then
  echo "Unknown HARDENED_CAMERA_SOURCE=${camera_source}; expected fake or real." >&2
  exit 1
fi
if [[ "${microphone_source}" != "fake" && "${microphone_source}" != "real" ]]; then
  echo "Unknown HARDENED_MICROPHONE_SOURCE=${microphone_source}; expected fake or real." >&2
  exit 1
fi

  extra_flags+=(
    --hardened-selectable-media-sources
    --hardened-default-camera-source="${camera_source}"
    --hardened-default-microphone-source="${microphone_source}"
    --hardened-private-camera-backend="${media_mode}"
    --hardened-private-camera-name="${obs_camera_name}"
  )

extra_flags+=(
  --hardened-default-location-source="${HARDENED_LOCATION_SOURCE:-fake}"
  --hardened-fake-location="${HARDENED_FAKE_LOCATION_LATITUDE:-28.6139},${HARDENED_FAKE_LOCATION_LONGITUDE:-77.2090},${HARDENED_FAKE_LOCATION_ACCURACY:-100}"
)
extra_flags+=(--hardened-privacy-rules-file="${website_view_file}")
extra_flags+=(--hardened-webdriver-mode="${webdriver_mode}")

if [[ -n "${HARDENED_FAKE_AUDIO_FILE:-}" ]]; then
  extra_flags+=(--use-file-for-fake-audio-capture="${HARDENED_FAKE_AUDIO_FILE}")
fi

if [[ "${media_mode}" == "loop" ]]; then
  loop_generator_args=(
    --output "${loop_video_file}"
    --width "${loop_video_width}"
    --height "${loop_video_height}"
    --frames "${loop_video_frames}"
    --fps "${loop_video_fps}"
  )
  if [[ "${HARDENED_LOOP_VIDEO_REGENERATE:-0}" == "1" ]]; then
    loop_generator_args+=(--force)
  fi
  if python3 "${script_directory}/generate_loop_video.py" "${loop_generator_args[@]}"; then
    extra_flags+=(--use-file-for-fake-video-capture="${loop_video_file}")
  else
    echo "Loop video is unavailable; using Chromium's synthetic private camera." >&2
  fi
elif [[ "${media_mode}" == "obs" ]]; then
  if [[ "${HARDENED_START_OBS:-1}" != "0" ]]; then
    if command -v "${obs_command}" >/dev/null 2>&1; then
      "${obs_command}" --startvirtualcam --minimize-to-tray \
        >"${obs_log}" 2>&1 &
      echo "Starting OBS Virtual Camera helper:"
      echo "  Command: ${obs_command} --startvirtualcam --minimize-to-tray"
      echo "  Log: ${obs_log}"
    else
      echo "OBS command not found: ${obs_command}" >&2
    fi
  fi
  python3 "${script_directory}/configure_obs_media.py" \
    --user-data-dir "${automation_profile}" \
    --profile-directory "${profile_directory}" \
    --camera-name "${obs_camera_name}"
  if [[ "${HARDENED_ENABLE_PIPEWIRE_CAMERA:-0}" == "1" ]]; then
    extra_flags+=(--enable-features=WebRtcPipeWireCamera)
  fi
elif [[ "${media_mode}" == "synthetic" ]]; then
  : # The selectable factory supplies Chromium's synthetic camera.
elif [[ "${media_mode}" != "system" && "${media_mode}" != "none" ]]; then
  echo "Unknown HARDENED_MEDIA_MODE=${media_mode}; expected loop, obs, synthetic, system, or none." >&2
  exit 1
fi

if [[ -e "${automation_profile}/SingletonLock" ]]; then
  echo "Warning: ${automation_profile} appears to be already running." >&2
  echo "Existing Chromium processes may ignore new media flags until restarted." >&2
fi

export CHROME_DEVEL_SANDBOX="${sandbox_helper}"

echo "Starting Hardened Chromium automation profile:"
echo "  Profile: ${automation_profile}"
echo "  Profile directory: ${profile_directory}"
if [[ "${backend_lock_held}" == "1" ]]; then
  echo "  Backend: private loopback CDP (${remote_debugging_address}:${remote_debugging_port})"
elif [[ "${backend_capable}" == "1" ]]; then
  echo "  Backend: reusing the existing default-profile process"
else
  echo "  Backend: disabled for named privacy-only profile ${named_profile}"
fi
echo "  WM_CLASS: ${wm_class}"
echo "  Media mode: ${media_mode}"
echo "  Camera default: ${camera_source} (${media_mode} backend)"
echo "  Microphone default: ${microphone_source}"
if [[ "${disable_vulkan}" != "0" ]]; then
  echo "  Graphics: Vulkan disabled for stable Wayland presentation feedback"
fi
if [[ "${media_mode}" == "loop" ]]; then
  echo "  Loop video: ${loop_video_file}"
fi
echo "Apps should use the Hardened Scrape Broker; raw CDP is private."

chromium_command=(
  "${chromium_binary}"
  --user-data-dir="${automation_profile}"
  --profile-directory="${profile_directory}"
  --class="${wm_class}"
  --no-first-run
  --no-default-browser-check
  "${backend_flags[@]}"
  "${extra_flags[@]}"
  "$@"
)

if [[ "${backend_lock_held}" == "1" ]]; then
  # Keep this small supervisor alive so the process-wide backend lock cannot
  # disappear when Chromium closes inherited file descriptors.
  "${chromium_command[@]}" &
  browser_pid=$!
  trap 'kill -TERM "${browser_pid}" 2>/dev/null || true' HUP INT TERM
  wait "${browser_pid}"
  exit $?
fi

exec "${chromium_command[@]}"
