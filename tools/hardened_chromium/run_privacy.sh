#!/bin/bash

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_directory="$(cd "${script_directory}/../.." && pwd)"
source "${script_directory}/performance_binary.sh"
chromium_binary="$(resolve_hardened_chromium_binary "${source_directory}" privacy)"
privacy_profile="${HARDENED_PRIVACY_PROFILE:-${source_directory}/out/HardenedPrivacyProfile}"
profile_directory="${HARDENED_CHROMIUM_PROFILE_DIRECTORY:-Default}"
website_view_file="${HARDENED_WEBSITE_VIEW_FILE:-${privacy_profile}/HardenedWebsiteView.json}"
sandbox_helper="${CHROME_DEVEL_SANDBOX:-/usr/local/sbin/chrome-devel-sandbox}"
wm_class="${HARDENED_CHROMIUM_WM_CLASS:-HardenedChromiumPrivacy}"

if [[ ! -x "${chromium_binary}" ]]; then
  echo "Hardened Chromium privacy binary not found: ${chromium_binary}" >&2
  exit 1
fi
if [[ ! -x "${sandbox_helper}" ]]; then
  echo "Chromium sandbox helper not found: ${sandbox_helper}" >&2
  echo "Install the compiled helper as described in BUILD.md." >&2
  exit 1
fi

product_validation_args=(
  --product privacy
  --binary "${chromium_binary}"
  --profile "${privacy_profile}"
)
if [[ "${HARDENED_ALLOW_PROFILE_SHARING:-0}" == "1" ]]; then
  product_validation_args+=(--allow-profile-sharing)
fi
if [[ "${HARDENED_ALLOW_UNVERIFIED_BINARY:-0}" == "1" ]]; then
  product_validation_args+=(--allow-unverified-binary)
fi
python3 "${script_directory}/hardened_product.py" \
  "${product_validation_args[@]}" >/dev/null
claim_hardened_profile_process_lock "${privacy_profile}" privacy

export CHROME_DEVEL_SANDBOX="${sandbox_helper}"
echo "Starting Hardened Chromium privacy profile:"
echo "  Profile: ${privacy_profile}"
echo "  Boundary: red"
echo "  Remote CDP: disabled by build"

chromium_command=(
  "${chromium_binary}"
  --user-data-dir="${privacy_profile}"
  --profile-directory="${profile_directory}"
  --class="${wm_class}"
  --no-first-run
  --no-default-browser-check
  --hardened-selectable-media-sources
  --hardened-default-camera-source=fake
  --hardened-default-microphone-source=fake
  --hardened-private-camera-backend=synthetic
  --hardened-default-location-source=fake
  --hardened-privacy-rules-file="${website_view_file}"
  "$@"
)

if [[ "${HARDENED_PROFILE_LOCK_HELD}" == "1" ]]; then
  "${chromium_command[@]}" &
  browser_pid=$!
  trap 'kill -TERM "${browser_pid}" 2>/dev/null || true' HUP INT TERM
  wait "${browser_pid}"
  exit $?
fi

exec "${chromium_command[@]}"
