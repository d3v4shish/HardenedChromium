# Hardened Chromium overlay

Engineering references: [build and validation](BUILD.md),
[architecture](ARCHITECTURE.md), [benchmarks](BENCHMARKS.md),
[hotspots](HOTSPOTS.md), and [current work](TODO.md).

This working tree is a pinned Chromium checkout carrying the Hardened Chromium
changes. The checked `patches/privacy` and `patches/automation` manifests export
those changes as ordered, checksummed overlays for another checkout.

The overlay was produced against Chromium revision
`f77d44b339946cd682d311c6c0bc922c32579fbd` (2026-08-03). A newer Chromium
revision is supported through the porting workflow in
[docs/hardened_chromium/PORTING.md](docs/hardened_chromium/PORTING.md).

## What it adds

- two build-time products: Privacy (red boundary, remote CDP disabled) and
  Automation (blue boundary, loopback CDP and broker enabled);
- hardened camera, microphone, and location source selection;
- a profile-owned Website View document with strict fake-source defaults,
  exact-origin overrides, and an expert Settings editor;
- distinct red Privacy and green Automation desktop icons, with red Privacy
  and blue Automation browser boundaries;
- one visible Chromium process shared safely by local applications;
- a loopback broker with REST, WebSocket, and SSE job APIs;
- application discovery, installation, authentication, diagnostics, and
  recovery tooling;
- feed generation in a separate process, plus performance and correctness
  gates.

## Product contracts

| Product | App icon | Boundary | Remote CDP | Broker/adapters | Default profile |
| --- | --- | --- | --- | --- | --- |
| Privacy | red | red | compiled to disabled stubs | unavailable | `out/HardenedPrivacyProfile` |
| Automation | green | blue | loopback, profile verified | available | `out/HardenedAutomationProfile` |

Automation includes a checksum-verified local adapter pack for X, LinkedIn,
Facebook, Reddit, and WhatsApp Web. Adapters read rendered DOM only. WhatsApp
is additionally restricted to the conversation panel below `#main`;
it cannot use target/account modes or capture a full-document MHTML snapshot.

Apply `patches/privacy` first. Apply `patches/automation` only on top of the
recorded Privacy bundle. Both manifests verify the pinned Chromium revision,
every source payload, base/output hashes, file modes, and the target's original
content before modifying it. Symlink destinations are rejected.

## Technical article series

For an implementation-level explanation of Chromium's architecture, the
website-visible signals changed by this fork, the exact hook points, and the
remaining detection limits, read:

1. [Where a Website Meets Chromium](ARTICLE_1_CHROMIUM_ARCHITECTURE.md)
2. [Changing What a Website Can Observe](ARTICLE_2_WEBSITE_VISIBLE_STATE.md)
3. [Protecting User Actions and Physical Devices](ARTICLE_3_USER_INPUT_MEDIA_PRIVACY.md)
4. [Website View, Automation Signals, and the Shared Browser](ARTICLE_4_AUTOMATION_POLICY_BROKER.md)

## Read this first

1. [Build and apply the overlay](docs/hardened_chromium/BUILDING.md)
2. [Run or integrate another application](docs/hardened_chromium/APP_INTEGRATION.md)
3. [Operate, diagnose, and recover the service](docs/hardened_chromium/OPERATIONS.md)
4. [Port the overlay to a newer Chromium revision](docs/hardened_chromium/PORTING.md)
5. [Understand feature behavior and validate a release](docs/hardened_chromium/FEATURES.md)
   and [VALIDATION.md](docs/hardened_chromium/VALIDATION.md)
6. [Copy working broker, privacy, and RSS examples](docs/hardened_chromium/EXAMPLES.md)
7. [Review Website View coverage and limitations](docs/hardened_chromium/FEATURES.md)

The detailed protocol and privacy-source references remain in
`tools/hardened_chromium/ARCHITECTURE.md` and
`tools/hardened_chromium/PRIVACY_SOURCES.md`.

## Support boundary

The broker is loopback-only and applications must use it rather than raw CDP.
Privacy and Automation use separate profiles. Launchers reject the other
product's profile marker unless `HARDENED_ALLOW_PROFILE_SHARING=1` is set as an
explicit, auditable override. That override permits sequential reuse only: an
active profile remains locked to the product process that owns it.

Install both user-local desktop entries after building the matching binaries:

```text
tools/hardened_chromium/install_red_desktop_entry.sh
tools/hardened_chromium/install_green_automation_desktop_entry.sh
python3 tools/hardened_chromium/install_hardened_chromium_service.py
```

Each entry preserves the verified binary selected at installation. The green
Automation icon is application identity only; its compiled trust boundary
remains blue.
