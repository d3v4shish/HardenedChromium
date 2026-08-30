#!/bin/bash

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_directory="$(cd "${script_directory}/../.." && pwd)"
source "${script_directory}/performance_binary.sh"
desktop_root="${XDG_DATA_HOME:-${HOME}/.local/share}"
applications_directory="${desktop_root}/applications"
desktop_file="${applications_directory}/hardened-chromium.desktop"
chromium_binary="$(resolve_hardened_chromium_binary "${source_directory}")"
launcher="${source_directory}/tools/hardened_chromium/run_for_automation.sh"
service_installer="${script_directory}/install_hardened_chromium_service.py"
icon="${source_directory}/out/Hardened/product_logo_48.png"
wm_class="${HARDENED_CHROMIUM_WM_CLASS:-HardenedChromium}"

if [[ ! -x "${chromium_binary}" ]]; then
  echo "Hardened Chromium binary not found: ${chromium_binary}" >&2
  exit 1
fi

if [[ ! -f "${icon}" ]]; then
  echo "Red Chromium icon not found: ${icon}" >&2
  exit 1
fi

if [[ ! -f "${service_installer}" ]]; then
  echo "Hardened Chromium service installer not found: ${service_installer}" >&2
  exit 1
fi

python3 "${service_installer}" --source-root "${source_directory}" --dry-run >/dev/null

mkdir -p "${applications_directory}"

cat > "${desktop_file}" <<EOF
[Desktop Entry]
Version=1.0
Name=Hardened Chromium
Comment=Hardened Chromium with local red Chromium icon
Exec=${launcher} %U
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

python3 "${service_installer}" --source-root "${source_directory}"

echo "Installed ${desktop_file}"
echo "Icon: ${icon}"
echo "StartupWMClass: ${wm_class}"
