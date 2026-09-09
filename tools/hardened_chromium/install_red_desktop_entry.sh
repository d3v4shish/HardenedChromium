#!/bin/bash

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_directory="$(cd "${script_directory}/../.." && pwd)"
source "${script_directory}/performance_binary.sh"
desktop_root="${XDG_DATA_HOME:-${HOME}/.local/share}"
applications_directory="${desktop_root}/applications"
icons_directory="${desktop_root}/icons/hicolor/scalable/apps"
desktop_file="${applications_directory}/hardened-chromium-privacy.desktop"
chromium_binary="$(resolve_hardened_chromium_binary "${source_directory}" privacy)"
launcher="${source_directory}/tools/hardened_chromium/run_privacy.sh"
source_icon="${script_directory}/icons/hardened-chromium-privacy.svg"
icon="${icons_directory}/hardened-chromium-privacy.svg"
wm_class="${HARDENED_CHROMIUM_WM_CLASS:-HardenedChromiumPrivacy}"

if [[ ! -x "${chromium_binary}" ]]; then
  echo "Hardened Chromium binary not found: ${chromium_binary}" >&2
  exit 1
fi

if [[ ! -f "${source_icon}" ]]; then
  echo "Privacy Chromium icon not found: ${source_icon}" >&2
  exit 1
fi

PYTHONPATH="${script_directory}" python3 - "${chromium_binary}" <<'PY'
import os
from pathlib import Path
import sys
from hardened_product import verify_binary_product

verify_binary_product(
    Path(sys.argv[1]), "privacy",
    allow_unverified=os.environ.get("HARDENED_ALLOW_UNVERIFIED_BINARY") == "1")
PY

mkdir -p "${applications_directory}" "${icons_directory}"
install -m 0644 "${source_icon}" "${icon}"

cat > "${desktop_file}" <<EOF
[Desktop Entry]
Version=1.0
Name=Hardened Chromium Privacy
Comment=Privacy browser with red trust boundary and remote CDP disabled
Exec=env HARDENED_CHROMIUM_BINARY=${chromium_binary} ${launcher} %U
Terminal=false
Type=Application
Icon=${icon}
StartupWMClass=${wm_class}
Categories=Network;WebBrowser;
MimeType=text/html;text/xml;application/xhtml+xml;x-scheme-handler/http;x-scheme-handler/https;
EOF

chmod 0644 "${desktop_file}"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${applications_directory}" >/dev/null 2>&1 || true
fi

echo "Installed ${desktop_file}"
echo "Icon: ${icon}"
echo "Binary: ${chromium_binary}"
echo "StartupWMClass: ${wm_class}"
