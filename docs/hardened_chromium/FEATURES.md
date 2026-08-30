# Feature guide and release validation

## Feature map

| Feature | User-facing behavior | Main implementation area |
| --- | --- | --- |
| Privacy sources | Real/private camera, microphone, and location selectors | content, media, permission UI |
| Browser roles | Blue shared-app boundary; red isolated-private boundary | browser views and frame code |
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

## Required release checks

Run these before publishing a build or port:

```sh
git diff --check
third_party/ninja/ninja -C out/Hardened chrome
cd tools/hardened_chromium
python3 -m unittest hardened_scrape_broker_test.py hardened_scrape_service_test.py \
  hardened_scrape_security_test.py hardened_scrape_performance_test.py \
  hardened_scrape_stream_test.py hardened_scrape_install_test.py
python3 benchmark_stream_transport.py --sockets 32 --events-per-second 1000 \
  --duration-seconds 3 --require-gates
python3 manual_multi_app_test.py --close-tabs
```

Run the manual test with a disposable profile when possible. It verifies
concurrent app startup convergence, one shared browser/backend, tabs rather
than per-app Chromium windows, output parsing, and event streaming.

## Manual privacy and crash checks

1. Open `tools/hardened_chromium/hardened_mode_test.html` in a named profile.
2. Request camera, microphone, and location separately and jointly.
3. Verify private defaults, real-source selector behavior, and ordinary
   allow/block prompts.
4. Open a default shared backend through `ensure`; verify the blue boundary.
5. Open a named private profile; verify the red boundary while privacy is on.
6. Submit a broker job, interact with its visible tab, then run diagnostics.
7. Confirm no new `FATAL`, `DCHECK failed`, `Received signal`, or app tab-open
   failure entries are present.

## Performance gates

The build-variant benchmark rejects a candidate with more than 5% regression
in a speed metric, more than 15% peak-RSS growth, or incomplete collection.
The transport gate requires lossless delivery, p95 event latency at most 25 ms,
and p99 at most 75 ms. Event fanout occurs before batched JSONL persistence;
do not move synchronous disk writes onto the WebSocket path.
