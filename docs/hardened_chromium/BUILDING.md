# Applying and building Hardened Chromium

## 1. Obtain the matching Chromium source

The safest base is Chromium revision
`f77d44b339946cd682d311c6c0bc922c32579fbd`. Follow Chromium's normal Linux
checkout instructions to create a `src` directory, then check out that
revision and synchronize dependencies:

```sh
cd /path/to/chromium/src
git checkout f77d44b339946cd682d311c6c0bc922c32579fbd
gclient sync -D
```

Do not replace Chromium's `.git` directory with this repository. This
repository deliberately contains only the overlay.

## 2. Apply the overlay

Clone this repository beside the Chromium checkout, fetch its `main` branch
into the Chromium repository, and restore every path supplied by the overlay.
Commit or otherwise back up local Chromium work first: this operation replaces
the corresponding upstream files.

```sh
git clone git@github.com:d3v4shish/HardenedChromium.git ../hardened-overlay
cd /path/to/chromium/src
git remote add hardened-overlay ../hardened-overlay
git fetch hardened-overlay main
git ls-tree -r --name-only hardened-overlay/main > /tmp/hardened-overlay-files
git restore --source hardened-overlay/main --pathspec-from-file=/tmp/hardened-overlay-files
git add --pathspec-from-file=/tmp/hardened-overlay-files
git commit -m "Apply Hardened Chromium overlay"
```

Review before committing with:

```sh
git diff --cached --stat
git diff --cached --check
```

For a private copy of the overlay, substitute its repository URL. Keep the
overlay remote separate from Chromium's `origin` so that upstream sync and
local hardened work remain unambiguous.

## 3. Configure a development build

The following GN arguments are a practical Linux development configuration.
They preserve DCHECKs while making iteration reasonable:

```sh
gn gen out/Hardened --args='is_debug=false
is_component_build=false
dcheck_always_on=true
symbol_level=1
proprietary_codecs=false
ffmpeg_branding="Chromium"'
third_party/ninja/ninja -C out/Hardened chrome
```

If depot_tools is initialized, `autoninja -C out/Hardened chrome` is also
valid. The repository's build helper uses `third_party/ninja/ninja`, so it
works even when `autoninja` is not on `PATH`.

The launchers expect the binary at `out/Hardened/chrome`. Set
`HARDENED_CHROMIUM_BINARY=/absolute/path/to/chrome` only when deliberately
using a different output directory.

## 4. Configure optimized variants (optional)

`tools/hardened_chromium/configure_performance_builds.py` creates portable and
Zen 4 optimized release variants. It needs Chromium's pinned PGO profiles;
use `--fetch-pgo` only on a trusted network where downloading those profiles
is permitted.

```sh
python3 tools/hardened_chromium/configure_performance_builds.py \
  --variant portable --fetch-pgo --build
python3 tools/hardened_chromium/configure_performance_builds.py \
  --variant zen4 --build
```

`HARDENED_CHROMIUM_VARIANT=auto` chooses a current compatible optimized binary
when one is available. `portable`, `zen4`, and `legacy` force a particular
choice. A Zen 4 binary must never be used on an incompatible CPU.

## 5. Install the local command

After building, install the desktop entry and stable discovery command:

```sh
tools/hardened_chromium/install_red_desktop_entry.sh
hardened-chromium-service capabilities --json
```

The installer writes a small user-local wrapper to `~/.local/bin`; it does
not copy Chromium or expose DevTools. Re-run the installer after moving the
source/build tree. To install only the command, use:

```sh
python3 tools/hardened_chromium/install_hardened_chromium_service.py
```

## 6. First launch and smoke test

```sh
hardened-chromium-service ensure --json
hardened-chromium-service diagnostics --json
```

For deliberate loopback no-auth operation, use `--no-auth` consistently on
every service invocation and client. Do not mix it with a token-authenticated
broker on the same port.
