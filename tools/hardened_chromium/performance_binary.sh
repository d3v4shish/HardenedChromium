#!/bin/bash

# Shared binary selection for Hardened Chromium launchers.

claim_hardened_profile_process_lock() {
  local profile="$1"
  local product="$2"
  local profile_lock="${profile}/.hardened-product.lock"
  local active_product=""

  HARDENED_PROFILE_LOCK_HELD=0
  if [[ -L "${profile_lock}" ]]; then
    echo "Profile product lock must not be a symlink: ${profile_lock}" >&2
    return 1
  fi
  exec 8>>"${profile_lock}"
  chmod 600 "${profile_lock}" 2>/dev/null || true
  if flock -n 8; then
    printf '%s\n' "${product}" >"${profile_lock}"
    HARDENED_PROFILE_LOCK_HELD=1
    return 0
  fi

  active_product="$(head -n 1 "${profile_lock}" 2>/dev/null || true)"
  if [[ "${active_product}" != "${product}" ]]; then
    echo "Profile ${profile} is active in the ${active_product:-unknown} product." >&2
    echo "Concurrent cross-product profile ownership is forbidden." >&2
    return 1
  fi
  # A same-product invocation may ask Chromium's own profile singleton to
  # open another window in the existing process.
}

hardened_cpu_supports_znver4() {
  [[ "$(uname -m)" == "x86_64" ]] || return 1
  [[ -r /proc/cpuinfo ]] || return 1

  local cpu_vendor cpu_flags required flag
  cpu_vendor="$(awk -F: '/^vendor_id[[:space:]]*:/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' /proc/cpuinfo)"
  [[ "${cpu_vendor}" == "AuthenticAMD" ]] || return 1
  cpu_flags=" $(awk -F: '/^flags[[:space:]]*:/ {print $2; exit}' /proc/cpuinfo) "
  required=(
    avx2 fma bmi1 bmi2
    avx512f avx512dq avx512cd avx512bw avx512vl
    avx512_bf16 avx512vbmi avx512_vbmi2 avx512_vnni
    avx512_bitalg avx512_vpopcntdq
  )
  for flag in "${required[@]}"; do
    [[ "${cpu_flags}" == *" ${flag} "* ]] || return 1
  done
}

hardened_manifest_variant() {
  local manifest="$1"
  [[ -f "${manifest}" ]] || return 1
  python3 - "${manifest}" <<'PY'
import json
import pathlib
import sys

try:
  value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
  selected = str(value.get("selectedVariant") or "").strip().lower()
  if selected in {"legacy", "portable", "zen4"}:
    print(selected)
except (OSError, ValueError, TypeError):
  pass
PY
}

resolve_hardened_chromium_binary() {
  local source_directory="$1"
  local product="${2:-privacy}"
  local explicit_binary="${HARDENED_CHROMIUM_BINARY:-}"
  local requested_variant="${HARDENED_CHROMIUM_CPU_VARIANT:-${HARDENED_CHROMIUM_VARIANT:-auto}}"
  local performance_root="${HARDENED_PERFORMANCE_ROOT:-${source_directory}/out/HardenedPerformance}"
  local manifest="${HARDENED_PERFORMANCE_MANIFEST:-${performance_root}/performance-results.json}"
  local product_suffix=""
  case "${product}" in
    privacy) product_suffix="Privacy" ;;
    automation) product_suffix="Automation" ;;
    *)
      echo "Unknown Hardened Chromium product: ${product}" >&2
      return 1
      ;;
  esac
  local legacy="${source_directory}/out/Hardened/chrome"
  local portable="${source_directory}/out/Hardened${product_suffix}/chrome"
  local zen4="${source_directory}/out/Hardened${product_suffix}Zen4/chrome"
  local selected=""

  if [[ -n "${explicit_binary}" ]]; then
    printf '%s\n' "${explicit_binary}"
    return 0
  fi

  case "${requested_variant}" in
    auto)
      selected="$(hardened_manifest_variant "${manifest}" || true)"
      if [[ "${selected}" == "zen4" ]] &&
         [[ -x "${zen4}" ]] &&
         hardened_cpu_supports_znver4; then
        printf '%s\n' "${zen4}"
        return 0
      fi
      if [[ "${selected}" == "portable" ]] &&
         [[ -x "${portable}" ]]; then
        printf '%s\n' "${portable}"
        return 0
      fi
      if [[ -x "${portable}" ]]; then
        printf '%s\n' "${portable}"
      else
        # Return the expected product path so the launcher emits a precise
        # missing-build error. Never silently cross the product boundary via
        # the historical single-binary output.
        printf '%s\n' "${portable}"
      fi
      ;;
    portable)
      printf '%s\n' "${portable}"
      ;;
    zen4)
      if ! hardened_cpu_supports_znver4; then
        echo "HARDENED_CHROMIUM_CPU_VARIANT=zen4 is incompatible with this CPU." >&2
        return 1
      fi
      printf '%s\n' "${zen4}"
      ;;
    legacy)
      if [[ "${HARDENED_ALLOW_LEGACY_BINARY:-0}" != "1" ]]; then
        echo "Legacy binary selection requires HARDENED_ALLOW_LEGACY_BINARY=1." >&2
        return 1
      fi
      printf '%s\n' "${legacy}"
      ;;
    *)
      echo "Unknown CPU variant ${requested_variant}; expected auto, portable, zen4, or legacy." >&2
      return 1
      ;;
  esac
}
