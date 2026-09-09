# Applying and building Hardened Chromium on Linux

This repository is an overlay, not a complete Chromium checkout. The procedure
below starts with a clean Linux machine, obtains the matching Chromium source,
applies the overlay, builds the browser and sandbox helper, and performs a
first launch.

Chromium's upstream Linux instructions are the authority for supported hosts
and build dependencies:
https://chromium.googlesource.com/chromium/src/+/main/docs/linux/build_instructions.md

## 1. Check the host

Use an x86-64 Linux host. Chromium development is primarily supported on
Ubuntu; other distributions may require equivalent packages or a container.
Plan for at least:

- 8 GB RAM, with more than 16 GB strongly recommended;
- 100 GB free disk space for one checkout and build (multiple optimized output
  directories need substantially more);
- Git, Python 3.9 or newer, and `sudo` access for installing build dependencies
  and the sandbox helper; and
- a checkout path with no spaces.

The build can take hours on a smaller machine. API keys are not required to
build or exercise the hardened features.

## 2. Install `depot_tools`

Clone Chromium's build tools and place their absolute path at the beginning of
`PATH`:

```sh
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git \
  /absolute/path/to/depot_tools
export PATH="/absolute/path/to/depot_tools:$PATH"
```

Persist the same `export` in the shell startup file used for builds. Use an
absolute path; do not put a literal `~` in `PATH`. Confirm the tools are found:

```sh
command -v fetch
command -v gclient
command -v gn
```

## 3. Obtain the matching Chromium source

The safest base is Chromium revision
`f77d44b339946cd682d311c6c0bc922c32579fbd`. Fetch the full history because a
shallow tip-of-tree checkout may not contain that revision:

```sh
mkdir -p /path/to/chromium
cd /path/to/chromium
fetch --nohooks chromium
cd src
git checkout f77d44b339946cd682d311c6c0bc922c32579fbd
gclient sync -D --nohooks \
  --revision src@f77d44b339946cd682d311c6c0bc922c32579fbd
./build/install-build-deps.sh
gclient runhooks
```

`install-build-deps.sh` targets Ubuntu and may prompt for `sudo`. On another
distribution, follow the equivalent package guidance in Chromium's upstream
Linux build instructions. The explicit `--revision` keeps the solution and its
dependencies on the supported source revision; running hooks afterward
downloads the matching toolchain and generated resources.

Do not replace Chromium's `.git` directory. Apply only the checked bundles from
the Hardened Chromium working tree.

## 4. Apply the overlay

Clone this repository beside the Chromium checkout, then apply the checked
Privacy bundle. Apply Automation only when that product is wanted. Commit or
otherwise back up local Chromium work first: this operation replaces the
corresponding upstream files.

```sh
git clone https://github.com/d3v4shish/HardenedChromium.git \
  /path/to/chromium/hardened-overlay
python3 /path/to/chromium/hardened-overlay/patches/apply.py privacy \
  --target /path/to/chromium/src
python3 /path/to/chromium/hardened-overlay/patches/apply.py automation \
  --target /path/to/chromium/src
```

Add `--check` to either command to validate revision, dependency state, and all
file checksums without writing. Automation requires both the Privacy state
marker and every Privacy payload file to match.

Review before committing with:

```sh
git diff --stat
git diff --check
```

For a private copy of the overlay, substitute its repository URL. Keep the
overlay remote separate from Chromium's `origin` so upstream sync and local
hardened work remain unambiguous. SSH users may substitute
`git@github.com:d3v4shish/HardenedChromium.git` for the HTTPS URL.

## 5. Configure and compile a development build

The following GN arguments are a practical Linux development configuration.
They preserve DCHECKs while making iteration reasonable:

```sh
gn gen out/HardenedPrivacyDev --args='is_debug=false
is_component_build=false
dcheck_always_on=true
symbol_level=1
proprietary_codecs=false
ffmpeg_branding="Chromium"
hardened_chromium_variant="privacy"'
third_party/ninja/ninja -C out/HardenedPrivacyDev chrome chrome_sandbox

gn gen out/HardenedAutomationDev --args='is_debug=false
is_component_build=false
dcheck_always_on=true
symbol_level=1
proprietary_codecs=false
ffmpeg_branding="Chromium"
hardened_chromium_variant="automation"'
third_party/ninja/ninja -C out/HardenedAutomationDev chrome chrome_sandbox
```

If `depot_tools` is initialized, this is equivalent:

```sh
autoninja -C out/HardenedPrivacyDev chrome chrome_sandbox
```

The repository's build helper uses `third_party/ninja/ninja`, so it also works
when `autoninja` is not on `PATH`. After native changes under `chrome`,
`content`, `components`, `media`, or Blink, rebuild before testing; Python-only
tests do not validate the C++ integration.

The launchers expect `out/HardenedPrivacy/chrome` or
`out/HardenedAutomation/chrome`. Set
`HARDENED_CHROMIUM_BINARY=/absolute/path/to/chrome` only when deliberately
using a different output directory. It must have a matching adjacent build
manifest; use `HARDENED_ALLOW_UNVERIFIED_BINARY=1` only for a local developer
binary.

## 6. Install the required Linux sandbox helper

Do not launch this browser with `--no-sandbox`. The hardened launchers require
an executable sandbox helper at `/usr/local/sbin/chrome-devel-sandbox` by
default. Install the helper produced by the same build:

```sh
sudo install -o root -g root -m 4755 \
  out/HardenedPrivacyDev/chrome_sandbox /usr/local/sbin/chrome-devel-sandbox
ls -l /usr/local/sbin/chrome-devel-sandbox
```

The listing must show owner `root`, group `root`, and mode `-rwsr-xr-x`. If a
different location is required, set it explicitly before using a launcher:

```sh
export CHROME_DEVEL_SANDBOX=/absolute/path/to/chrome-devel-sandbox
```

Reinstall the helper after rebuilding `chrome_sandbox` or switching to a build
whose sandbox API version differs. Chromium's
`build/update-linux-sandbox.sh` is an alternative installer; set
`BUILDTYPE=HardenedPrivacyDev` (or `HardenedAutomationDev`) when using it with
the corresponding output directory.

## 7. Configure optimized variants (optional)

`tools/hardened_chromium/configure_performance_builds.py` creates portable and
Zen 4 optimized release variants. It needs Chromium's pinned PGO profiles; use
`--fetch-pgo` only on a trusted network where downloading those profiles is
permitted.

```sh
python3 tools/hardened_chromium/configure_performance_builds.py \
  --product privacy --variant portable --fetch-pgo --build
python3 tools/hardened_chromium/configure_performance_builds.py \
  --product automation --variant portable --build
```

`HARDENED_CHROMIUM_CPU_VARIANT=auto` chooses a current compatible optimized binary
when one is available. `portable`, `zen4`, and `legacy` force a particular
choice. A Zen 4 binary must never be used on an incompatible CPU. Optimized
variants use their own output directories but can use the sandbox helper from
section 6 when they were built from the same pinned source revision.

## 8. Install the local command

After building, install the red Privacy desktop entry, green Automation desktop
entry, and Automation discovery command separately:

```sh
tools/hardened_chromium/install_red_desktop_entry.sh
tools/hardened_chromium/install_green_automation_desktop_entry.sh
python3 tools/hardened_chromium/install_hardened_chromium_service.py
hardened-chromium-service capabilities --json
```

The installer writes a small user-local wrapper to `~/.local/bin`; it does not
copy Chromium or expose DevTools. Re-run the installer after moving the
source/build tree. To install only the command, use:

```sh
python3 tools/hardened_chromium/install_hardened_chromium_service.py
```

Ensure `~/.local/bin` is on `PATH` if the shell cannot find
`hardened-chromium-service` after installation.
The desktop entries have separate application IDs and WM classes. Their red
and green icons identify Privacy and Automation respectively; Automation's
compiled browser boundary remains blue.

## 9. Understand the profile policy and private CDP endpoint

The automation profile owns `HardenedWebsiteView.json`. Its baseline defaults
camera, microphone, and location to fake sources, while exact HTTP(S)-origin
rules can override those values. Ordinary browser permission prompts still
apply. Edit the document at `chrome://settings/privacy` under **Website view**,
or use the authenticated broker's `GET` and `POST /service/website-view`
endpoints.

The normal launchers use a loopback-only, non-zero CDP port (`9222` by default)
and hide `navigator.webdriver`. These settings can be overridden explicitly:

```sh
HARDENED_REMOTE_DEBUGGING_PORT=9333 \
HARDENED_WEBDRIVER_MODE=report \
tools/hardened_chromium/run_for_automation.sh
```

The port must be non-zero, unused, and bound only to `127.0.0.1` or `::1`.
`HARDENED_WEBSITE_VIEW_FILE=/absolute/path/to/HardenedWebsiteView.json` selects
an alternate policy document. Reload a website after changing source rules;
restart Chromium after changing the automation mode or CDP launch settings.
See [FEATURES.md](FEATURES.md) for the fields the current browser actually
enforces.

## 10. First launch and smoke test

```sh
hardened-chromium-service ensure --json
hardened-chromium-service diagnostics --json
```

For deliberate loopback no-auth operation, use `--no-auth` consistently on
every service invocation and client. Do not mix it with a token-authenticated
broker on the same port.

Then run the static, Python, native-build, and manual release checks in
[VALIDATION.md](VALIDATION.md). A successful build alone does not validate the
privacy-source, Website View, shared-browser, or broker behavior.

## Troubleshooting the build

- If `fetch`, `gclient`, or `gn` is missing, put the absolute `depot_tools`
  directory at the beginning of `PATH` and start a new shell.
- If GN reports missing toolchains or generated files, run `gclient sync -D`
  and `gclient runhooks` again from `/path/to/chromium/src`.
- If the compiler is killed, reduce Ninja parallelism, for example
  `autoninja -C out/HardenedPrivacyDev -j 2 chrome chrome_sandbox`, or add
  RAM/swap. Repeat for `out/HardenedAutomationDev`.
- If a launcher reports that the sandbox helper is missing or out of date,
  rebuild `chrome_sandbox` and repeat section 6.
- If port `9222` is occupied, stop the conflicting local process or choose one
  unused non-zero loopback port consistently for the service and launcher.
