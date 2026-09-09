# Privacy and Automation product split

## User-local product installation

- [x] Add and install distinct product desktop identities.
  Acceptance: Privacy installs as a separate desktop application with a red
  icon and `HardenedChromiumPrivacy` WM class; Automation installs separately
  with a green icon and `HardenedChromiumAutomation` WM class while retaining
  its compiled blue browser boundary.
- [x] Pin every installed entry and the broker service to a verified matching
  product binary.
  Acceptance: desktop launches preserve the binary selected during install;
  the service wrapper preserves its verified Automation binary; neither can
  silently fall back to the historical single-product build.
- [x] Validate and document the two-product installation.
  Acceptance: installer tests, shell syntax checks, desktop-file validation,
  product-manifest verification, and installed service capabilities pass; the
  legacy single desktop entry is retired without losing a recoverable copy.

- [x] Establish and document the current correctness and performance baseline.
  Acceptance: existing tests and benchmarks are recorded before product-split measurements.
- [x] Add checked, ordered Privacy and Automation patch bundles.
  Acceptance: Privacy applies to the pinned Chromium base; Automation requires Privacy; revision and checksum mismatches fail before applying.
- [x] Add deterministic Privacy and Automation GN/build configurations and manifests.
  Acceptance: both variants produce distinct output directories and identify their variant at runtime.
- [x] Enforce red-only Privacy and blue-only Automation browser boundaries.
  Acceptance: colors are derived from the compiled variant and cannot be changed by launch switches.
- [x] Disable remote CDP transports in Privacy while retaining internal DevTools.
  Acceptance: port, pipe, and approval-mode requests create no listener or `DevToolsActivePort`; Automation CDP remains profile verified.
- [x] Add profile-role metadata and guarded cross-product profile sharing.
  Acceptance: default profiles are separate; cross-product reuse requires an explicit path and override; concurrent ownership is rejected.
- [x] Extract site collection into a checksummed, local-only adapter-pack framework.
  Acceptance: deterministic domain selection, crawl modes, bounded operation, generic fallback, and explicit-adapter failure are tested.
- [x] Implement default X, LinkedIn, Facebook, Reddit, and WhatsApp adapters.
  Acceptance: offline fixtures cover current, scope, targets, and account behavior without write actions or secret extraction.
- [x] Extend broker APIs, events, job records, and exports with adapter and completion metadata.
  Acceptance: existing clients remain compatible and invalid adapter requests return stable 4xx errors.
- [x] Build, test, profile, and benchmark both products and adapter collection.
  Acceptance: native/Python/integration suites pass and measured results satisfy documented gates.
  - [x] Generate both GN configurations and verify compiled product flags.
  - [x] Compile changed production objects for both products.
  - [x] Complete full Privacy and Automation `chrome` builds.
  - [x] Compile and run the focused native product/geolocation tests.
  - [x] Re-run the complete Python suite after sealing final manifests.
  - [x] Record the baseline, CPU profile, and post-change benchmark gates.
  - [x] Complete disposable-profile Privacy and Automation product smoke.
- [ ] Complete authenticated live-site acceptance for every supported adapter
  view and crawl mode.
  Acceptance: controlled accounts exercise X, LinkedIn, Facebook, Reddit, and
  the selected WhatsApp conversation; artifacts are reviewed for scope and
  WhatsApp exclusion rules without performing write actions.
- [x] Update all required architecture, build, benchmark, hotspot, migration, and release documentation.
  Acceptance: a clean checkout can reproduce patch application, both builds, tests, and benchmarks using documented commands.

## Completed stabilization prerequisite

- [x] Canonicalize and validate Website View rules without discarding retained-only expert fields.
- [x] Verify profile-owned DevTools endpoints and reject mismatched listeners.
- [x] Bound durable event persistence and expose storage failures.
