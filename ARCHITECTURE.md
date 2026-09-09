# Architecture

Hardened Chromium is one source overlay with two compile-time product
contracts. `hardened_chromium_variant="privacy"` produces an always-red browser
whose remote port, pipe, and approval paths return or execute disabled stubs.
Internal, browser-owned DevTools remains available. The `automation` value
produces an always-blue browser and retains remote CDP for the local broker.
Runtime switches do not select the color or product.
Desktop identity is separate from the content boundary: Privacy uses a red
application icon and Automation uses a green application icon. Distinct
desktop IDs and WM classes prevent the shell from grouping the two roles.
Automation's `navigator.webdriver` launch override is also guarded by the
compiled product flag, so it is absent from Privacy builds even after both
overlays have been applied to one checkout.

The checked patch order is:

```text
pinned Chromium -> Privacy bundle -> Automation bundle
```

Each build emits an adjacent product manifest. Each default user-data directory
contains `.hardened-profile.json`; launchers and the service reject a role
mismatch unless explicit sharing is enabled. A launcher-held advisory lock
records the active product and rejects concurrent cross-product ownership;
same-product window requests remain subject to Chromium's profile singleton.

## Automation data flow

```text
local app -> loopback authenticated broker -> profile-verified CDP
          -> visible browser tab -> local adapter -> normalized artifacts
```

`tools/hardened_chromium` contains the automation supervisor, scrape broker,
adapter pack, and Website View policy model. The browser profile is the
authority for both Website View state and DevTools discovery.

The launcher writes Chrome's `DevToolsActivePort` in the profile. Before the
service reuses an endpoint, it requires a live browser process for that
profile, the marker port, and a matching browser GUID from `/json/version`.
This prevents attaching to an unrelated listener on the default port.

The broker accepts HTTP requests, publishes events through `EventHub`, and
persists them to job and global JSONL logs. Event payloads are encoded once per
publication and shared by WebSocket subscribers. Per-subscriber queues remain
bounded. The durable writer also has message and byte limits; producers
backpressure until disk catches up, and a write failure becomes observable in
the health response rather than silently dropping events.

Settings WebUI reads and writes Website View JSON in the profile. It accepts
HTTP(S) URL input for rules but persists canonical origins. Currently enforced
controls are camera, microphone, location, and the default automation setting;
other policy fields remain stored for future enforcement.

## Adapter boundary

The broker loads `adapter_packs/default/manifest.json` once at startup and
verifies the SHA-256 digest of every schema before accepting jobs. Domain
matching is deterministic and suffix-safe. Built-in jobs default to `scope`;
`current`, explicit URL `targets`, and confirmed `account` discovery are also
available where declared. All loops share item, target, no-progress, and wall
time bounds.

Adapters only query rendered DOM and perform scrolling/navigation. They do not
submit forms, mutate page state, or read browser credential storage. WhatsApp
has the narrower invariant that roots, fallback text, HTML, and visible-text
snapshots use the rendered conversation panel below `#main`. If no conversation
panel exists, it collects nothing. Full-document MHTML and conversation
switching are disabled for it. Collection and raw capture fail closed if a
navigation leaves the selected adapter's declared domains.
