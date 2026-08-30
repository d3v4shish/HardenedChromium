#!/bin/bash

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_directory="$(cd "${script_directory}/../.." && pwd)"
source "${script_directory}/performance_binary.sh"
chromium_binary="$(resolve_hardened_chromium_binary "${source_directory}")"
virtual_media_profile="${HARDENED_VIRTUAL_MEDIA_PROFILE:-${source_directory}/out/HardenedVirtualMediaProfile}"
sandbox_helper="${CHROME_DEVEL_SANDBOX:-/usr/local/sbin/chrome-devel-sandbox}"

if [[ ! -x "${chromium_binary}" ]]; then
  echo "Hardened Chromium binary not found: ${chromium_binary}" >&2
  exit 1
fi

if [[ ! -x "${sandbox_helper}" ]]; then
  echo "Chromium sandbox helper not found: ${sandbox_helper}" >&2
  echo "Install the compiled helper as described in the project README." >&2
  exit 1
fi

export CHROME_DEVEL_SANDBOX="${sandbox_helper}"

# Replace platform camera and microphone capture with Chromium's deterministic
# test devices. Permission is not auto-granted: websites still trigger the
# browser-controlled camera/microphone prompt. A separate profile ensures an
# already-running normal browser cannot absorb this process-wide option.
exec "${chromium_binary}" \
  --user-data-dir="${virtual_media_profile}" \
  --no-first-run \
  --no-default-browser-check \
  --use-fake-device-for-media-stream \
  "$@"
