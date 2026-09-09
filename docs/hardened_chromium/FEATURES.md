# Feature guide and release validation

## Feature map

| Feature | User-facing behavior | Main implementation area |
| --- | --- | --- |
| Privacy sources | Real/private camera, microphone, and location selectors | content, media, permission UI |
| Website View | Profile baseline plus exact-origin website-visible policy | Settings, profile JSON, content, Blink |
| Browser products | Red Privacy build with remote CDP disabled; blue Automation build with broker CDP | build flags, DevTools server, browser frame code |
| Shared backend | One visible default-profile Chromium; broker opens tabs | launcher, service, broker |
| Application API | REST jobs plus replayable WebSocket/SSE events | `hardened_scrape_broker.py` |
| Feed/output derivation | RSS/Atom/JSON Feed/CSV/HTML/JSON after collection | broker child process |
| Recovery and logs | Lifecycle, app-open events, crash diagnosis | service + broker |
| Performance variants | portable/Zen 4 builds with gated benchmarks | build config and tools |
| Hardened interaction behavior | Input, selection, clipboard, idle, screen changes | Blink and content |

## Feed behavior

The broker, not the user-facing Chromium UI, performs dynamic page collection.
Apps submit a URL and optional extraction schema. The broker opens a tab,
renders/scans/scrolls it, writes normalized items and raw snapshots, and
derives output feeds in a short-lived child process. A user makes a website
feed-capable by creating a feed/job or saving an extraction schema in the
broker UI/API; no separate RSS process runs inside every Chromium instance.

## Website View

`chrome://settings/privacy` contains a **Website view** editor backed by
`HardenedWebsiteView.json` in the active profile. It has one profile baseline
and exact HTTP(S)-origin rules: `https://example.test` does not apply to a
subdomain, a different port, or an embedded third party. The default baseline
uses fake camera, microphone, and location sources; a missing or malformed
document also fails closed to those launcher/default fake sources.

The document intentionally retains arbitrary expert fields. The broker's
`GET /service/website-view` endpoint returns warnings for values the current
browser build does not implement; it never rewrites them into a different
identity. The browser currently enforces these parts:

- default and exact-origin camera, microphone, and location sources;
- `navigator.webdriver` as `hide` or `report` in Automation builds, selected
  at browser start and compiled out of Privacy builds; and
- a non-zero, loopback-only CDP port (`9222` by default), avoiding the
  automation marker associated with Chromium's port-zero launch.

Persona, canvas, WebGL, audio, WebRTC, font, client-hint, and related exposure
keys are persisted now as the stable configuration contract, but are warnings
until their matching engine hooks are implemented. Do not treat an unimplemented
key as a privacy guarantee. Reload websites after source-policy changes;
restart Chromium after changing the automation mode or CDP launch settings.

## Required release checks

Run these before publishing a build or port:

```sh
git diff --check
third_party/ninja/ninja -C out/HardenedPrivacyDev chrome
third_party/ninja/ninja -C out/HardenedAutomationDev chrome
cd tools/hardened_chromium
PYTHONPATH=. python3 -m unittest discover -p '*_test.py'
PYTHONPATH=. python3 benchmark_adapters.py --iterations 20000 --require-gates
python3 benchmark_stream_transport.py --sockets 32 --events-per-second 1000 \
  --duration-seconds 3 --require-gates
python3 manual_multi_app_test.py --close-tabs
```

Run the manual test with a disposable profile when possible. It verifies
concurrent app startup convergence, one shared browser/backend, tabs rather
than per-app Chromium windows, output parsing, and event streaming.

## Manual privacy and crash checks

1. Open `tools/hardened_chromium/hardened_mode_test.html` in the Privacy build.
2. Request camera, microphone, and location separately and jointly.
3. Verify private defaults, real-source selector behavior, and ordinary
   allow/block prompts.
4. Open a default shared Automation backend through `ensure`; verify the blue boundary.
5. Open the Privacy binary with `run_privacy.sh`; verify the red boundary and
   that remote port, pipe, and approval requests create no listener.
6. Submit a broker job, interact with its visible tab, then run diagnostics.
7. Confirm no new `FATAL`, `DCHECK failed`, `Received signal`, or app tab-open
   failure entries are present.

## Performance gates

The build-variant benchmark rejects a candidate with more than 5% regression
in a speed metric, more than 15% peak-RSS growth, or incomplete collection.
The transport gate requires lossless delivery, p95 event latency at most 25 ms,
and p99 at most 75 ms. Event fanout occurs before batched JSONL persistence;
do not move synchronous disk writes onto the WebSocket path.
