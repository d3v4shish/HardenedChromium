# Shared browser and application broker

`run_for_automation.sh` owns the default profile at
`out/HardenedAutomationProfile`. Its first invocation starts one visible
Chromium process with private loopback DevTools access on an ephemeral port.
Broker jobs open as tabs in that shared visible backend process; they do not
create a Chromium process or top-level window per application.
The launcher keeps a per-user file lock for the lifetime of the first process,
so simultaneous launches cannot create competing application backends.

The content boundary communicates the browser role. The app-accessible backend
has a 3 px blue boundary, including while hardened privacy is active. A named
private browser has the existing 3 px red boundary when hardened privacy is
active. Blue therefore means "app controlled," while red means "private user
browsing." All app tabs share the backend profile, including its website
sessions, cookies, cache, and privacy settings.

Applications never receive the DevTools address. They use
`hardened_scrape_service.py --json ensure` as an idempotent discover-or-start
operation and submit work to the authenticated loopback broker. The helper
serializes concurrent callers, reuses a healthy browser and broker, and starts
each missing process at most once. `status --json` performs the same readiness
probe without starting anything. Named profiles can be opened with:

```sh
tools/hardened_chromium/run_for_automation.sh --named-profile research
```

Named profiles retain the native privacy features but do not expose a browser
backend and cannot be used by broker applications.

## Installing and discovering the app backend

Install the browser desktop entry and the app-discovery command together:

```sh
tools/hardened_chromium/install_red_desktop_entry.sh
```

This installs `hardened-chromium-service` in `~/.local/bin`. It is a stable
user-local wrapper around this Hardened Chromium source/build; it does not
expose CDP. Third-party apps must discover the backend through the command,
not by scanning browser processes or loopback ports:

```sh
hardened-chromium-service capabilities --json
hardened-chromium-service ensure --json
```

`capabilities` never starts Chromium and returns the backend identity
`hardened-chromium-broker`, protocol version, and supported features. Apps
should use Playwright or another fallback if the command is absent, reports an
incompatible protocol, or `ensure` fails. `ensure` is safe for concurrent
callers and may start the one visible shared backend when it is not running.
If Chromium was closed while an old broker is still listening, `ensure` starts
a replacement browser, retires that broker's stale CDP connection, and starts
a broker for the new browser. `AutoBrokerClient` performs this check before
each new job or feed run; non-Python clients should invoke `ensure --json`
immediately before submitting new browser work.

The development-tree wrapper can be installed directly without the desktop
entry and supports a temporary/custom prefix for testing:

```sh
python3 tools/hardened_chromium/install_hardened_chromium_service.py
python3 tools/hardened_chromium/install_hardened_chromium_service.py --uninstall
```

The default desktop/automation profile is therefore shared: a user-opened
window and all app-opened tabs belong to the same `HardenedAutomationProfile`
Chromium process and see the same cookies, logins, cache, extensions, and
privacy configuration. A private `--named-profile` is intentionally separate
and is never shared with apps.

## Application integration

Apps should keep one `AutoBrokerClient` and one WebSocket for their lifetime.
REST is the command path; the WebSocket multiplexes events for every job owned
by that app. Apps do not spawn Chromium directly and should not use CDP:

```python
from hardened_scrape_client import AutoBrokerClient

client = AutoBrokerClient(
    app_id="my-app",
    # In production, provision an app secret once and pass app_secret here.
)
job = client.submit_job("https://example.com")["job"]

# The cursor is per job. Zero replays retained events; persist the latest
# sequence after processing each event and reuse it after reconnecting.
with client.open_event_stream({job["id"]: 0}) as events:
  while message := events.receive_json():
    if message.get("jobId") == job["id"]:
      print(message)
```

`AutoBrokerClient` calls the locked discover-or-start helper, so any number of
simultaneously starting applications converge on one broker and one visible
Chromium process. The broker creates a tab (`/json/new`) in that process for
each job. Chromium decides whether a user gesture opens another tab or a
top-level window, but either is still part of the same process/profile; the
broker deliberately uses tabs to avoid multiplying windows and renderer UI.

For a provisioned application, authenticate REST with
`X-Hardened-App-Id` and `X-Hardened-App-Secret`. `POST /stream-tickets`
returns a 30-second, single-use WebSocket URL carrying the same app scope.
Send this after connecting to subscribe or resume several jobs:

```json
{"type":"subscribe","subscriptions":[
  {"jobId":"job-a","afterSequence":81},
  {"jobId":"job-b","afterSequence":12}
]}
```

Every data event has `type`, `jobId`, `appId`, monotonically increasing
per-job `sequence`, `timeEpoch`, and `data`. Replay and live fanout share one
atomic boundary, so an event cannot be lost or duplicated during subscribe.
Retained replay is bounded; `resync_required` tells an app to fetch current
REST state and resume from `latestSequence`. A connection that stops reading
is closed with WebSocket code 1013 instead of consuming memory without bound.
The compatibility SSE endpoint is
`GET /jobs/<id>/events?afterSequence=<sequence>` and also accepts the standard
`Last-Event-ID` header. It uses the same event-driven hub, not a polling loop.

The CLI exposes both transports:

```sh
python3 hardened_scrape_client.py events JOB_ID --websocket --after-sequence 0
python3 hardened_scrape_client.py events JOB_ID --stream --after-sequence 0
```

To exercise discovery, concurrent process startup, shared PIDs, distinct tabs,
WebSocket completion events, and real page parsing together, run:

```sh
python3 manual_multi_app_test.py --close-tabs
```

## Process ownership

- `hardened_scrape_service.py stop` stops only the broker. It leaves every
  browser window open.
- `hardened_scrape_service.py restart` restarts/reconciles only the broker and
  reuses the browser whenever it is alive.
- Closing the shared default browser is deliberately separate and confirmed:

  ```sh
  tools/hardened_chromium/hardened_scrape_service.py \
    stop-browser --confirm-shared-browser
  ```

- Browser-side feed/archive collection is not installed. Dynamic collection
  happens through broker-owned tabs. Derived CSV, HTML, JSON Feed, RSS, and
  Atom generation runs in a short-lived child process, globally serialized so
  checkpoints cannot multiply memory pressure.

## Scheduling and recovery

The broker defaults to eight active jobs total and two per application. Queues
are round-robin across application identities. Override the limits with
`--max-active-jobs` and `--max-active-jobs-per-app` (or their corresponding
`HARDENED_BROKER_...` environment variables).

Queued jobs remain queued across broker restarts. A job that was active when
the broker exited is recorded as `interrupted` and can be retried with
`POST /jobs/<job_id>/retry`. Completed tabs stay open for inspection until an
application calls `POST /jobs/<job_id>/close-tab` or bulk-cleans its terminal
tabs with `POST /jobs/close-completed-tabs`.

Jobs, schemas, and feeds are application-owned. Application credentials can
list and operate only on their own resources; the service administrator can
inspect all resources. `GET /jobs/<job_id>` returns metadata only, while item
polling uses the paginated `GET /jobs/<job_id>/items` endpoint.

## Performance and correctness checks

Run the dependency-free broker tests from this directory:

```sh
cd tools/hardened_chromium
python3 -m unittest hardened_scrape_broker_test.py \
  hardened_scrape_service_test.py hardened_scrape_security_test.py \
  hardened_scrape_performance_test.py hardened_scrape_stream_test.py
```

The build-variant benchmark rejects a candidate with more than a 5% regression
in any speed metric, more than 15% peak-RSS growth, or incomplete dynamic
collection.

The event transport has a separate real-loopback gate (it does not require a
browser):

```sh
python3 benchmark_stream_transport.py --sockets 32 \
  --events-per-second 1000 --duration-seconds 3 --require-gates
```

It requires lossless delivery with p95 at most 25 ms and p99 at most 75 ms.
On the 2026-08-26 implementation run it delivered all 3,000 events with
0.124 ms p95, 0.257 ms p99, and 0.386 ms maximum latency. TCP `NODELAY` is set
on both ends, streaming fanout precedes asynchronous batched JSONL persistence,
and each connection has bounded message/byte queues.
