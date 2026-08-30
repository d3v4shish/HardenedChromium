#!/bin/bash

# Shared binary selection for Hardened Chromium launchers.

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

hardened_binary_is_current() {
  local candidate="$1"
  local baseline="$2"
  [[ -x "${candidate}" ]] || return 1
  [[ ! -x "${baseline}" || ! "${baseline}" -nt "${candidate}" ]]
}

resolve_hardened_chromium_binary() {
  local source_directory="$1"
  local explicit_binary="${HARDENED_CHROMIUM_BINARY:-}"
  local requested_variant="${HARDENED_CHROMIUM_VARIANT:-auto}"
  local performance_root="${HARDENED_PERFORMANCE_ROOT:-${source_directory}/out/HardenedPerformance}"
  local manifest="${HARDENED_PERFORMANCE_MANIFEST:-${performance_root}/performance-results.json}"
  local legacy="${source_directory}/out/Hardened/chrome"
  local portable="${source_directory}/out/HardenedPortable/chrome"
  local zen4="${source_directory}/out/HardenedZen4/chrome"
  local selected=""

  if [[ -n "${explicit_binary}" ]]; then
    printf '%s\n' "${explicit_binary}"
    return 0
  fi

  case "${requested_variant}" in
    auto)
      selected="$(hardened_manifest_variant "${manifest}" || true)"
      if [[ "${selected}" == "zen4" ]] &&
         hardened_binary_is_current "${zen4}" "${legacy}" &&
         hardened_cpu_supports_znver4; then
        printf '%s\n' "${zen4}"
        return 0
      fi
      if [[ "${selected}" == "portable" ]] &&
         hardened_binary_is_current "${portable}" "${legacy}"; then
        printf '%s\n' "${portable}"
        return 0
      fi
      if [[ "${selected}" == "legacy" ]] && [[ -x "${legacy}" ]]; then
        printf '%s\n' "${legacy}"
        return 0
      fi
      if hardened_binary_is_current "${portable}" "${legacy}"; then
        printf '%s\n' "${portable}"
      else
        printf '%s\n' "${legacy}"
      fi
      ;;
    portable)
      printf '%s\n' "${portable}"
      ;;
    zen4)
      if ! hardened_cpu_supports_znver4; then
        echo "HARDENED_CHROMIUM_VARIANT=zen4 is incompatible with this CPU." >&2
        return 1
      fi
      printf '%s\n' "${zen4}"
      ;;
    legacy)
      printf '%s\n' "${legacy}"
      ;;
    *)
      echo "Unknown HARDENED_CHROMIUM_VARIANT=${requested_variant}; expected auto, portable, zen4, or legacy." >&2
      return 1
      ;;
  esac
}
