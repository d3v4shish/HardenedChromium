# Integrating local applications

## Contract

Applications discover `hardened-chromium-service` on `PATH`; they do not scan
processes, guess a port, launch Chromium, or connect to CDP. The service owns
one visible default-profile Chromium backend and returns the broker URL and,
when token mode is used, an administrator token.

```sh
hardened-chromium-service capabilities --json
hardened-chromium-service ensure --json
```

`capabilities` is read-only. `ensure` is serialized across callers and either
reuses the healthy backend or starts the missing browser/broker components.
Treat `browser_unreachable` as a user-action-required error: a visible browser
already owns the profile but is not servicing private CDP. Do not kill it from
an application.

## Python client

Keep one `AutoBrokerClient` per application lifetime. It calls `ensure` before
new work, so recovery occurs before a tab is requested.

```python
from hardened_scrape_client import AutoBrokerClient

client = AutoBrokerClient(app_id="reader")
job = client.submit_job(
    "https://example.com",
    max_items=200,
    raw_snapshots=True,
)["job"]

with client.open_event_stream({job["id"]: 0}) as stream:
    while event := stream.receive_json():
        if event.get("type") == "item":
            consume(event["data"]["item"])
```

The Python implementation is in `tools/hardened_chromium/hardened_scrape_client.py`.
Run its CLI help for the full job, schema, feed, and event commands.

## HTTP and JavaScript clients

`broker_connect_example.js` is the complete Node example. Its sequence is:

1. run `hardened-chromium-service --json ensure`;
2. submit `POST /jobs` with `{"url":"https://..."}`;
3. create `POST /stream-tickets` using the same authentication;
4. connect to the short-lived returned WebSocket URL;
5. subscribe using per-job sequence cursors.

Use the service-returned broker URL, not a hard-coded port. REST creates or
queries work; WebSocket is the low-latency multiplexed event transport. SSE
is available for compatibility at `GET /jobs/<id>/events`.

## Authentication choices

Token mode is the default. Local applications may pass the service token in
`Authorization: Bearer ...`. For least privilege, provision an application
through the broker UI/API and send:

```text
X-Hardened-App-Id: <id>
X-Hardened-App-Secret: <secret>
```

Application credentials scope jobs, feeds, schemas, and event streams to that
application. Never put a durable secret in a WebSocket URL; request a
single-use stream ticket first.

No-auth mode is loopback-only and intended for trusted single-user setups:

```sh
hardened-chromium-service --no-auth ensure --json
```

An authenticated and a no-auth broker cannot coexist at the same URL. Choose
one mode before integration and keep it stable.

## Job and stream lifecycle

Submitting a job produces `job_accepted`, then the broker emits `tab_opening`,
`tab_opened`, navigation, item, status, and output events. Persist each
per-job `sequence`; reconnect with `afterSequence` after interruption. If the
broker sends `resync_required`, fetch the REST job state and resume at the
reported latest sequence.

The broker opens a tab in the one visible backend process. Tabs share that
profile's sessions and privacy configuration. Finished tabs stay available for
user inspection until an application closes them explicitly.

## Scraping versus parsing

The broker navigates, renders, scrolls, captures page state, applies an
optional extraction schema, writes raw/normalized artifacts, and derives RSS,
Atom, JSON Feed, HTML, CSV, and JSON outputs. The caller specifies an optional
schema or consumes the normalized items; it does not parse Chromium's DOM
itself. Feed/archive derivation runs in a separate short-lived process so it
does not compete with browser rendering or event fanout.
