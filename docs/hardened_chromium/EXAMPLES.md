# Working examples

These examples use the installed `hardened-chromium-service`, rather than a
Chromium executable or a CDP port. It discovers or starts the one local,
visible app-backend browser, then returns the local broker endpoint. The
browser has feeds disabled; extraction and feed generation happen outside the
browser process.

All URLs, selectors, and fake coordinates below are examples. Replace them
with values you control or are permitted to process.

## 1. Run a broker job from an application

For a trusted single-user computer, this is the smallest end-to-end test. It
starts/reuses the local service in loopback-only no-auth mode, requests a page,
and prints each normalized extracted item as it arrives over one WebSocket.

```python
#!/usr/bin/env python3
from pathlib import Path
import sys

# In a source checkout. An installed application can instead install or vendor
# hardened_scrape_client.py and simply import it.
sys.path.insert(0, str(Path("tools/hardened_chromium").resolve()))

from hardened_scrape_client import AutoBrokerClient

client = AutoBrokerClient(app_id="example-reader", no_auth=True)
job = client.submit_job(
    "https://example.com",
    max_items=25,
    timeout_seconds=45,
    raw_snapshots=False,
)["job"]

with client.open_event_stream({job["id"]: 0}) as stream:
    while event := stream.receive_json():
        print(event["type"], event.get("data", {}))
        if event.get("type") == "status" and event["data"].get("status") in {
            "completed", "failed", "stopped", "interrupted",
        }:
            break
```

Run it from the Chromium source root after saving it as, for example,
`/tmp/broker_example.py`:

```sh
python3 /tmp/broker_example.py
```

An application normally runs in token mode (the default) or uses a provisioned
application ID and secret. Omit `no_auth=True` in that case; the client runs
`ensure`, receives the local token, and keeps it out of WebSocket URLs. The
complete Node.js variant is
[`tools/hardened_chromium/broker_connect_example.js`](../../tools/hardened_chromium/broker_connect_example.js).
For credential and lifecycle details, see [APP_INTEGRATION.md](APP_INTEGRATION.md).

### Equivalent REST request

No-auth mode must remain loopback-only. It is useful to inspect the protocol
locally, not to expose the broker to a LAN, proxy, or the public internet.

```sh
hardened-chromium-service --no-auth ensure --json
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","app_id":"example-cli","max_items":25}' \
  http://127.0.0.1:8877/jobs
```

The response contains `job.id`. For a small compatibility client, fetch
`GET /jobs/<job-id>/events` as Server-Sent Events. For normal streaming, use
`POST /stream-tickets` and the returned short-lived WebSocket URL as the Python
and Node examples do.

## 2. Set privacy sources and verify the boundary

There are two independent visual states:

| Browser use | Boundary | Meaning |
| --- | --- | --- |
| User private browser / privacy protection enabled | Red | private browsing context and hardened privacy state |
| Tab opened by a local application through the broker | Blue | shared app-backend tab in the one visible Chromium process |

The broker holds the global location defaults and the per-origin camera,
microphone, and location rules. Privacy mutation APIs require an administrator
token in normal operation. In a local no-auth test, the following configures
fake media and a fixed fake location, then adds a site-specific rule:

```sh
BROKER=http://127.0.0.1:8877

curl --fail-with-body -X POST "$BROKER/service/privacy-settings" \
  -H 'Content-Type: application/json' \
  -d '{
    "media": {
      "cameraSource": "fake",
      "microphoneSource": "fake",
      "fakeCameraBackend": "loop"
    },
    "location": {
      "source": "fake",
      "latitude": 28.6139,
      "longitude": 77.2090,
      "accuracy": 100
    }
  }'

curl --fail-with-body -X POST "$BROKER/service/privacy-rules" \
  -H 'Content-Type: application/json' \
  -d '{
    "origin": "https://example.com",
    "cameraSource": "fake",
    "microphoneSource": "fake",
    "locationSource": "fake",
    "fakeCameraBackend": "loop"
  }'

curl --fail-with-body "$BROKER/service/privacy-settings"
```

For token mode, add `-H "Authorization: Bearer $HARDENED_BROKER_TOKEN"` to
each request. The configuration is persisted by the service and applied by the
browser privacy-source implementation; it does not require each calling app to
manage device permission prompts. The source and boundary behavior is covered
in [PRIVACY_SOURCES.md](../../tools/hardened_chromium/PRIVACY_SOURCES.md) and
[FEATURES.md](FEATURES.md).

## 3. Make a website RSS-, Atom-, JSON Feed-, and CSV-capable

An RSS feed is a saved extraction schema plus a scheduled or on-demand broker
job. The browser renders and extracts the page. Once extraction is complete,
a separate short-lived feed worker produces the feed files, so formatting does
not block Chromium rendering, CDP work, or WebSocket event fanout.

Start from the ready-to-edit definition:

[`example-feed.json`](examples/example-feed.json)

It says that each `main article` is an item, with fields selected relative to
that item. Set `sourceUrl`, `itemRoot`, and every selector for the target site.
Use the broker UI's selector picker or `pick_selector` API to derive selectors
from a rendered page before saving a feed.

Save and run it through the provided CLI:

```sh
hardened-chromium-service --no-auth ensure --json

python3 tools/hardened_chromium/hardened_scrape_client.py \
  save-feed --file docs/hardened_chromium/examples/example-feed.json

python3 tools/hardened_chromium/hardened_scrape_client.py \
  run-feed example-news --wait --max-items 50
```

After the terminal `completed` event, download all generated outputs to one
directory:

```sh
python3 tools/hardened_chromium/hardened_scrape_client.py \
  download-feed example-news --out /tmp/example-news
```

The directory contains `feed.rss`, `feed.atom`, `feed.json`, `items.csv`,
`items.jsonl`, `latest.json`, `latest.html`, and `visible_text.txt` when those
artifacts were produced by the source page.

The same sequence from an app is concise:

```python
feed = client.save_feed({
    "id": "my-news",
    "name": "My news",
    "sourceUrl": "https://example.com/news",
    "itemRoot": "article",
    "fields": [
        {"name": "title", "selector": "h2", "mode": "text", "required": True},
        {"name": "link", "selector": "a[href]", "mode": "attr:href"},
    ],
})["feed"]
job = client.run_feed(feed["id"], max_items=50)["job"]
# Consume job events exactly as in example 1; after completed:
rss_bytes = client.download_latest_feed_file(feed["id"], "feed.rss")
```

Feed ownership follows the calling app's credentials in token/application mode.
No-auth mode has no such isolation and is therefore suitable only where all
local processes are mutually trusted. The complete feed, scheduling, and
output contract is described in
[`tools/hardened_chromium/ARCHITECTURE.md`](../../tools/hardened_chromium/ARCHITECTURE.md).

## Test checklist

1. Start exactly one service with `hardened-chromium-service --no-auth ensure --json`.
2. Run the broker example twice. Both jobs should open tabs in the same visible,
   blue-boundary app backend—not extra browser processes.
3. Apply the privacy example, open a matching site, and verify fake permission
   sources and the expected red private boundary where privacy mode is enabled.
4. Edit the sample feed to a permitted site, save it, run it, and inspect each
   downloaded output in `/tmp`.
5. Close Chromium, submit another app job, and confirm `ensure` recreates the
   backend rather than leaving callers stuck. Check
   `hardened-chromium-service diagnostics --json` if it does not.
