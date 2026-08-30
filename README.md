# Hardened Chromium overlay

This repository is an **overlay**, not a Chromium mirror. It contains every
file added or changed by Hardened Chromium, but intentionally omits Chromium's
unchanged upstream files and history. Start with Chromium, then apply this
overlay to obtain a buildable tree.

The overlay was produced against Chromium revision
`f77d44b339946cd682d311c6c0bc922c32579fbd` (2026-08-03). A newer Chromium
revision is supported through the porting workflow in
[docs/hardened_chromium/PORTING.md](docs/hardened_chromium/PORTING.md).

## What it adds

- hardened camera, microphone, and location source selection;
- clear red/private and blue/app-backend browser boundaries;
- one visible Chromium process shared safely by local applications;
- a loopback broker with REST, WebSocket, and SSE job APIs;
- application discovery, installation, authentication, diagnostics, and
  recovery tooling;
- feed generation in a separate process, plus performance and correctness
  gates.

## Read this first

1. [Build and apply the overlay](docs/hardened_chromium/BUILDING.md)
2. [Run or integrate another application](docs/hardened_chromium/APP_INTEGRATION.md)
3. [Operate, diagnose, and recover the service](docs/hardened_chromium/OPERATIONS.md)
4. [Port the overlay to a newer Chromium revision](docs/hardened_chromium/PORTING.md)
5. [Understand feature behavior and validate a release](docs/hardened_chromium/FEATURES.md)
   and [VALIDATION.md](docs/hardened_chromium/VALIDATION.md)

The detailed protocol and privacy-source references remain in
`tools/hardened_chromium/ARCHITECTURE.md` and
`tools/hardened_chromium/PRIVACY_SOURCES.md`.

## Support boundary

The broker is loopback-only and applications must use it rather than raw CDP.
The shared backend profile intentionally shares cookies, sessions, cache,
extensions, and browser privacy settings with app-created tabs. Use a named
profile for a private, non-shared browser session.
