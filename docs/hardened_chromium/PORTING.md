# Porting to a newer Chromium revision

## Goal and safety rule

Port the overlay as a series of reviewed feature groups, not as a blind file
copy. Chromium evolves rapidly around media, permissions, compositor, Blink,
and performance-manager APIs. A clean merge is not proof that behavior or
threading contracts remain correct.

Always create a branch from the intended Chromium revision and preserve a
known-good build before starting:

```sh
cd /path/to/chromium/src
git fetch origin
git switch -c hardened-port origin/main
git log -1 --oneline
```

## Recommended sequence

1. Fetch this overlay as a remote and inspect its changed-path list.
2. Apply small coherent groups: build switches, content/privacy, media,
   browser UI, lifecycle/performance, Blink behavior, then tools/docs/assets.
3. After each group, run `git diff --check`, generate GN, and compile the
   nearest target before moving on.
4. Resolve API changes according to current Chromium ownership and sequence
   rules; do not reintroduce obsolete calls merely to make an old patch apply.
5. Run focused unit/browser tests, then the broker/service suite and a manual
   visible-browser test.

The affected areas are visible with:

```sh
git diff --name-only f77d44b339946cd682d311c6c0bc922c32579fbd HEAD
```

## High-risk porting points

### Privacy-rule cache

`content/browser/geolocation/hardened_privacy_source.cc` must keep all file
I/O off permission and UI callbacks. It uses a MayBlock worker and publishes a
locked in-memory snapshot. Never replace that with `ScopedAllowBlocking` on a
permission path. Preserve fake-until-loaded behavior when the rules file is
configured.

### Media and permission UI

Media device selection crosses content, browser permission UI, and capture
factories. Confirm real/fake choices still preserve normal permission checks,
that labels remain generic where intended, and that camera and microphone can
be selected independently.

### App boundary and lifecycle

Blue Automation/red Privacy boundary behavior depends on the generated product
build flag and browser frame/view code. Test both compiled variants after any
UI refactor. Performance-manager changes must retain their existing test
coverage; they affect discard/freeze semantics.

### Blink/input changes

The overlay modifies Blink document, selection, input, screen, clipboard, and
idle behavior. Rebase these against semantic changes, not textual line offsets.
Run the nearby Blink tests named by the changed directories.

## Build and test loop

```sh
gn gen out/HardenedPrivacyDev --args='is_debug=false is_component_build=false dcheck_always_on=true symbol_level=1 hardened_chromium_variant="privacy"'
gn gen out/HardenedAutomationDev --args='is_debug=false is_component_build=false dcheck_always_on=true symbol_level=1 hardened_chromium_variant="automation"'
third_party/ninja/ninja -C out/HardenedPrivacyDev chrome
third_party/ninja/ninja -C out/HardenedAutomationDev chrome
cd tools/hardened_chromium
PYTHONPATH=. python3 -m unittest discover -p '*_test.py'
```

Then run `manual_multi_app_test.py --close-tabs` against a disposable profile,
exercise location/media prompts with `hardened_mode_test.html`, and inspect
`hardened-chromium-service diagnostics --json` for new fatal indicators.

## Updating the documented base

After a successful port, record the new Chromium revision in the root README,
commit the port separately from feature work, and publish an updated overlay
snapshot. Do not claim compatibility with `main` without this validation.
