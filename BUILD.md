# Build, run, test, and benchmark

Run commands from this repository's `src` directory.

## Apply the ordered overlays

Starting from Chromium revision
`f77d44b339946cd682d311c6c0bc922c32579fbd`:

```text
python3 /path/to/overlay/patches/apply.py privacy --target /path/to/chromium/src
python3 /path/to/overlay/patches/apply.py automation --target /path/to/chromium/src
```

The second command is optional. It fails unless the verified Privacy state
and files are present. Neither command accepts changed target files. Add
`--check` to perform every verification without writing. GN also refuses the
Automation variant unless the second overlay's presence sentinel exists.

## Build

Release builds use pinned Chromium and V8 PGO profiles:

```text
python3 tools/hardened_chromium/configure_performance_builds.py --product privacy --variant portable --build
python3 tools/hardened_chromium/configure_performance_builds.py --product automation --variant portable --build
```

This produces `out/HardenedPrivacy/chrome` and
`out/HardenedAutomation/chrome`, each with an adjacent
`hardened-build-manifest.json`. A manifest remains `built: false` until Ninja
succeeds, then records the binary SHA-256; launchers reject incomplete,
mismatched, or modified builds. Add `--fetch-pgo` only when the pinned profiles
are absent and network access is available. Use `--variant zen4` only for an
explicit host-specific build.

For a deterministic non-PGO developer configuration:

```text
buildtools/linux64/gn gen out/HardenedPrivacyDev --args='is_debug=false symbol_level=0 hardened_chromium_variant="privacy" hardened_chromium_cpu_tuning="portable"'
buildtools/linux64/gn gen out/HardenedAutomationDev --args='is_debug=false symbol_level=0 hardened_chromium_variant="automation" hardened_chromium_cpu_tuning="portable"'
third_party/ninja/ninja -C out/HardenedPrivacyDev chrome
third_party/ninja/ninja -C out/HardenedAutomationDev chrome
```

## Run

```text
tools/hardened_chromium/run_privacy.sh about:blank
tools/hardened_chromium/run_for_automation.sh about:blank
python3 tools/hardened_chromium/hardened_scrape_service.py ensure --json
```

Launchers require a matching build manifest. Local development binaries can
opt out with `HARDENED_ALLOW_UNVERIFIED_BINARY=1`; release automation must not.

## Install

Install the independently named desktop applications and the Automation broker
command:

```text
tools/hardened_chromium/install_red_desktop_entry.sh
tools/hardened_chromium/install_green_automation_desktop_entry.sh
python3 tools/hardened_chromium/install_hardened_chromium_service.py
hardened-chromium-service capabilities --json
```

Privacy uses a red icon and Automation uses a green icon. Automation retains
its blue in-browser boundary. The entries and service wrapper preserve the
verified product binaries selected during installation.

## Test

```text
cd tools/hardened_chromium
PYTHONPATH=. python3 -m unittest discover -p '*_test.py'
```

Native product checks:

```text
third_party/ninja/ninja -C out/HardenedPrivacyDev unit_tests
out/HardenedPrivacyDev/unit_tests --gtest_filter='RemoteDebuggingServerTest.*:HardenedBrowserViewTest.*:HardenedAppBackendBrowserViewTest.*:AppBackendWithoutPrivacyBrowserViewTest.*:StandardBrowserViewTest.AlwaysShowsCompiledProductBorder'
third_party/ninja/ninja -C out/HardenedAutomationDev unit_tests
out/HardenedAutomationDev/unit_tests --gtest_filter='RemoteDebuggingServerTest.*:HardenedBrowserViewTest.*:HardenedAppBackendBrowserViewTest.*:AppBackendWithoutPrivacyBrowserViewTest.*:StandardBrowserViewTest.AlwaysShowsCompiledProductBorder'
third_party/ninja/ninja -C out/HardenedPrivacyDev content_unittests
out/HardenedPrivacyDev/content_unittests --gtest_filter='HardenedPrivacySourceTest.*'
```

With a correctly installed setuid sandbox helper, smoke-test both real binaries
with disposable profiles and loopback-only ephemeral ports:

```text
python3 tools/hardened_chromium/product_smoke.py \
  --privacy-binary out/HardenedPrivacyDev/chrome \
  --automation-binary out/HardenedAutomationDev/chrome
```

The command fails if Privacy creates a CDP listener or `DevToolsActivePort`, or
if Automation's endpoint port and browser GUID do not match its profile marker.

## Benchmark and profile

```text
cd tools/hardened_chromium
PYTHONPATH=. python3 benchmark_adapters.py --iterations 20000 --require-gates
PYTHONPATH=. python3 benchmark_stream_transport.py --sockets 32 --events-per-second 1000 --duration-seconds 3 --require-gates
PYTHONPATH=. python3 -m cProfile -s cumulative benchmark_adapters.py --iterations 20000 --require-gates
```

The integration tests and benchmarks bind loopback sockets. Run them in an
environment that permits local TCP listeners.

## Recorded validation (2026-09-09)

The non-PGO smoke outputs used
`is_debug=false is_component_build=false symbol_level=0`, portable CPU tuning,
and their respective hardened variant:

```text
third_party/ninja/ninja -C out/hardened-privacy-smoke -j 8 chrome
third_party/ninja/ninja -C out/hardened-automation-smoke -j 8 chrome
```

Both current-tree builds regenerated Ninja files and exited successfully after
linking `chrome` (Privacy 43,994 actions; Automation 44,003 actions on the
initial full builds). The monolithic `unit_tests` runners also built
successfully at 7,194 actions per variant. The documented focused filters then
passed 10/10 Privacy tests and 9/9 Automation tests. Privacy
`content_unittests` built successfully and `HardenedPrivacySourceTest.*` passed
2/2 tests.

After the desktop-identity installation change and final manifest generation,
the complete command under **Test** passed 103/103 Python and loopback
integration tests in 8.737 seconds. Both installed desktop files passed
`desktop-file-validate`; their copied red/green SVG checksums matched the
source assets, both product binary manifests passed checksum verification, and
the installed service reported the verified Automation binary with no missing
components.

The checked-in `product_smoke.py` command passed with an installed root-owned
mode-4755 sandbox helper: Privacy initialized without a CDP listener or marker;
Automation's loopback endpoint, nonzero port marker, and browser GUID agreed.
Authenticated live-site adapter checks were not run; they remain explicit in
`TODO.md` and are not implied by offline fixture coverage.
